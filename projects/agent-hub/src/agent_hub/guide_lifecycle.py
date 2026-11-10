"""Append-only maintenance of published guides without changing frozen release evidence."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, Query, Request
from pydantic import Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import guides, repositories
from . import schemas as S
from .models import Base, Identity, ProjectProgress
from .security import HubError
from .work_policy import digest


class Amendment(Identity, Base):
    __tablename__ = "guide_amendments"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guide_jobs.id"))
    base_version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(2000))


class AmendmentReview(Identity, Base):
    __tablename__ = "guide_amendment_reviews"
    amendment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guide_amendments.id"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    gate: Mapped[str] = mapped_column(String(16))
    approved: Mapped[bool]
    reason: Mapped[str] = mapped_column(String(2000))
    override_reason: Mapped[str] = mapped_column(String(2000))


class Publication(Base):
    __tablename__ = "guide_publications"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guide_jobs.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    action: Mapped[str] = mapped_column(String(16))
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("guide_amendments.id"))
    reason: Mapped[str] = mapped_column(String(2000))


class Feedback(Identity, Base):
    __tablename__ = "guide_feedback"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guide_jobs.id"))
    publication_version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(String(4000))


class Resolution(Identity, Base):
    __tablename__ = "guide_feedback_resolutions"
    feedback_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guide_feedback.id"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(2000))


class AmendmentInput(S.Input):
    base_version: int = Field(ge=0)
    content: guides.GuideContent
    reason: str = Field(min_length=10, max_length=2000)


class ReviewInput(S.Input):
    gate: Literal["product", "test"]
    approved: bool
    reason: str = Field(min_length=10, max_length=2000)
    override_reason: str = Field(default="", max_length=2000)


class PublicationInput(S.Input):
    expected_version: int = Field(ge=0)
    action: Literal["published", "archived", "restored"]
    amendment_id: uuid.UUID | None = None
    reason: str = Field(min_length=10, max_length=2000)


class FeedbackInput(S.Input):
    publication_version: int = Field(ge=0)
    kind: Literal["issue", "suggestion", "outdated"]
    body: str = Field(min_length=10, max_length=4000)


class ResolutionInput(S.Input):
    state: Literal["open", "resolved", "rejected"]
    reason: str = Field(min_length=10, max_length=2000)


def current(db, job_id):
    return db.scalar(
        select(Publication).where(Publication.job_id == job_id).order_by(Publication.version.desc()).limit(1)
    )


def overlay(db, job):
    publication = current(db, job.id) if job.state == "published" else None
    amendment = (
        db.get(Amendment, publication.amendment_id) if publication and publication.amendment_id else None
    )
    return publication, amendment


def content_expression():
    """Only approved publication content participates in directory searches."""
    return func.coalesce(
        select(Amendment.content)
        .join(Publication, Publication.amendment_id == Amendment.id)
        .where(Publication.job_id == guides.GuideJob.id)
        .order_by(Publication.version.desc())
        .limit(1)
        .correlate(guides.GuideJob)
        .scalar_subquery(),
        guides.GuideRevision.content,
    )


def lock(db, job):
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('guide-publication:'||:id,0))"),
        {"id": str(job.id)},
    )


def check_current(db, job, expected, *, allow_archived=False):
    if job.state != "published":
        raise HubError("GUIDE_NOT_PUBLISHED", 409)
    previous = current(db, job.id)
    version = previous.version if previous else 0
    if expected != version:
        raise HubError("GUIDE_PUBLICATION_VERSION_CONFLICT", 409, current_version=version)
    if not allow_archived and previous and previous.action == "archived":
        raise HubError("GUIDE_ARCHIVED", 409)
    return previous


def amendment_view(db, row, *, include_private):
    result = {
        "id": str(row.id),
        "base_version": row.base_version,
        "created_at": row.created_at.isoformat(),
        "content": row.content,
        "content_hash": row.content_hash,
    }
    if include_private:
        reviews = list(
            db.scalars(
                select(AmendmentReview)
                .where(AmendmentReview.amendment_id == row.id)
                .order_by(AmendmentReview.created_at)
            )
        )
        result.update(
            author_id=str(row.author_id),
            reason=row.reason,
            reviews=[
                {
                    "gate": r.gate,
                    "approved": r.approved,
                    "reason": r.reason,
                    "author_id": str(r.author_id),
                    "override_reason": r.override_reason,
                }
                for r in reviews
            ],
        )
    return result


def event(hub, db, identity, row, kind, request_id, **payload):
    # Release events have a unique frozen job version. Maintenance uses its own
    # append-only records and progress events without rewriting that evidence.
    hub.audit(
        db,
        identity.user.id,
        "guide." + kind,
        row.id,
        request_id,
        row.project_id,
        repository_id=row.repository_id,
        **payload,
    )
    db.add(
        ProjectProgress(
            project_id=row.project_id,
            repository_id=row.repository_id,
            author_id=identity.user.id,
            device_id=identity.device.id,
            status="checkpoint",
            summary="功能操作说明 · " + kind,
            detail="",
            task_reference=str(row.task_id),
            task_id=row.task_id,
            kind="guide." + kind,
        )
    )


def install(app, hub, actor, mutate, dependency):
    DB = Annotated[object, Depends(dependency, scope="function")]

    def access(project_id, job_id, request, db, cap=None):
        identity = actor(request, db)
        hub.project(db, identity, project_id, cap or "project:read")
        job = guides.scoped_get(db, project_id, job_id, request, require_private=bool(cap))
        if job.state != "published":
            raise HubError("GUIDE_NOT_PUBLISHED", 409)
        if cap:
            if cap == "tasks:write" and job.repository_id:
                repositories.require(db, project_id, job.repository_id, "code:write")
            lock(db, job)
        return identity, job

    def get_amendment(db, job, amendment_id):
        row = db.get(Amendment, amendment_id)
        if not row or row.job_id != job.id:
            raise HubError("GUIDE_AMENDMENT_NOT_ACCESSIBLE", 404)
        return row

    @app.get("/api/projects/{project_id}/guides/{job_id}/amendments")
    def amendments(
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(20, ge=1, le=100),
    ):
        _, job = access(project_id, job_id, request, db)
        rows = list(
            db.scalars(
                select(Amendment)
                .where(Amendment.job_id == job.id)
                .order_by(Amendment.created_at.desc(), Amendment.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return {
            "items": [amendment_view(db, r, include_private=guides.private(db, job)) for r in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_offset": offset + min(len(rows), limit),
        }

    @app.get("/api/projects/{project_id}/guides/{job_id}/amendments/{amendment_id}")
    def read_amendment(
        project_id: uuid.UUID, job_id: uuid.UUID, amendment_id: uuid.UUID, request: Request, db: DB
    ):
        _, job = access(project_id, job_id, request, db)
        return amendment_view(
            db, get_amendment(db, job, amendment_id), include_private=guides.private(db, job)
        )

    @app.post("/api/projects/{project_id}/guides/{job_id}/amendments")
    def amend(project_id: uuid.UUID, job_id: uuid.UUID, body: AmendmentInput, request: Request, db: DB):
        identity, job = access(project_id, job_id, request, db, "tasks:write")

        def save():
            check_current(db, job, body.base_version)
            value = body.content.model_dump(mode="json")
            row = Amendment(
                job_id=job.id,
                base_version=body.base_version,
                author_id=identity.user.id,
                device_id=identity.device.id,
                content=value,
                content_hash=digest(value),
                reason=body.reason,
            )
            db.add(row)
            db.flush()
            event(
                hub,
                db,
                identity,
                job,
                "amendment_created",
                request.state.request_id,
                amendment_id=str(row.id),
            )
            return amendment_view(db, row, include_private=True)

        return mutate(request, db, identity, body, save)

    @app.post("/api/projects/{project_id}/guides/{job_id}/amendments/{amendment_id}/decisions")
    def review(
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        amendment_id: uuid.UUID,
        body: ReviewInput,
        request: Request,
        db: DB,
    ):
        identity, job = access(
            project_id, job_id, request, db, "tasks:test" if body.gate == "test" else "tasks:publish"
        )
        row = get_amendment(db, job, amendment_id)

        def save():
            check_current(db, job, row.base_version)
            if row.author_id == identity.user.id:
                if identity.user.platform_role != "superadmin" or len(body.override_reason.strip()) < 10:
                    raise HubError("GUIDE_INDEPENDENT_REVIEW_REQUIRED", 403)
            elif body.override_reason:
                raise HubError("GUIDE_OVERRIDE_NOT_APPLICABLE", 422)
            if db.scalar(
                select(AmendmentReview.id).where(
                    AmendmentReview.amendment_id == row.id, AmendmentReview.gate == body.gate
                )
            ):
                raise HubError("GUIDE_GATE_ALREADY_DECIDED", 409)
            db.add(
                AmendmentReview(
                    amendment_id=row.id,
                    author_id=identity.user.id,
                    device_id=identity.device.id,
                    **body.model_dump(),
                )
            )
            event(
                hub,
                db,
                identity,
                job,
                "amendment_reviewed",
                request.state.request_id,
                amendment_id=str(row.id),
                gate=body.gate,
                approved=body.approved,
            )
            db.flush()
            return amendment_view(db, row, include_private=True)

        return mutate(request, db, identity, body, save)

    @app.get("/api/projects/{project_id}/guides/{job_id}/publications")
    def history(
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(20, ge=1, le=100),
    ):
        _, job = access(project_id, job_id, request, db)
        rows = list(
            db.scalars(
                select(Publication)
                .where(Publication.job_id == job.id)
                .order_by(Publication.version.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        items = [
            {
                "version": r.version,
                "action": r.action,
                "created_at": r.created_at.isoformat(),
                "amendment_id": str(r.amendment_id) if r.amendment_id else None,
                **({"reason": r.reason, "author_id": str(r.author_id)} if guides.private(db, job) else {}),
            }
            for r in rows[:limit]
        ]
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_offset": offset + min(len(rows), limit),
            "original_release_available": True,
        }

    @app.post("/api/projects/{project_id}/guides/{job_id}/publications")
    def publish(project_id: uuid.UUID, job_id: uuid.UUID, body: PublicationInput, request: Request, db: DB):
        identity, job = access(project_id, job_id, request, db, "tasks:publish")

        def save():
            previous = check_current(db, job, body.expected_version, allow_archived=body.action == "restored")
            amendment_id = previous.amendment_id if previous else None
            if body.action == "published":
                if not body.amendment_id:
                    raise HubError("GUIDE_AMENDMENT_REQUIRED", 422)
                amendment = get_amendment(db, job, body.amendment_id)
                check_current(db, job, amendment.base_version)
                approved = set(
                    db.scalars(
                        select(AmendmentReview.gate).where(
                            AmendmentReview.amendment_id == amendment.id, AmendmentReview.approved.is_(True)
                        )
                    )
                )
                if approved != {"product", "test"}:
                    raise HubError("GUIDES_NOT_APPROVED", 409)
                amendment_id = amendment.id
            elif body.amendment_id is not None:
                raise HubError("GUIDE_ARCHIVE_CONTENT_FROZEN", 422)
            if body.action == "restored" and (not previous or previous.action != "archived"):
                raise HubError("GUIDE_NOT_ARCHIVED", 409)
            db.add(
                Publication(
                    job_id=job.id,
                    version=body.expected_version + 1,
                    action=body.action,
                    amendment_id=amendment_id,
                    reason=body.reason,
                    author_id=identity.user.id,
                    device_id=identity.device.id,
                )
            )
            event(
                hub,
                db,
                identity,
                job,
                "catalog_" + body.action,
                request.state.request_id,
                catalog_version=body.expected_version + 1,
            )
            db.flush()
            return guides.view(db, job, content=True)

        return mutate(request, db, identity, body, save)

    @app.get("/api/projects/{project_id}/guides/{job_id}/publications/{publication_version}")
    def read_publication(
        project_id: uuid.UUID, job_id: uuid.UUID, publication_version: int, request: Request, db: DB
    ):
        _, job = access(project_id, job_id, request, db)
        publication = db.get(Publication, (job.id, publication_version)) if publication_version > 0 else None
        if publication_version < 0 or publication_version > 0 and not publication:
            raise HubError("GUIDE_PUBLICATION_NOT_ACCESSIBLE", 404)
        source = (
            db.get(Amendment, publication.amendment_id)
            if publication and publication.amendment_id
            else db.get(guides.GuideRevision, (job.id, job.draft_version))
        )
        return {
            "version": publication_version,
            "action": publication.action if publication else "original",
            "content": source.content,
            "content_hash": source.content_hash,
            "content_trust": "reviewed-project-data",
        }

    @app.get("/api/projects/{project_id}/guides/{job_id}/feedback")
    def feedback(
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(20, ge=1, le=100),
    ):
        _, job = access(project_id, job_id, request, db)
        rows = list(
            db.scalars(
                select(Feedback)
                .where(Feedback.job_id == job.id)
                .order_by(Feedback.created_at.desc(), Feedback.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        items = []
        for row in rows[:limit]:
            resolution = db.scalar(
                select(Resolution)
                .where(Resolution.feedback_id == row.id)
                .order_by(Resolution.created_at.desc(), Resolution.id.desc())
                .limit(1)
            )
            items.append(
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "body": row.body,
                    "publication_version": row.publication_version,
                    "created_at": row.created_at.isoformat(),
                    "state": resolution.state if resolution else "open",
                    "resolution": resolution.reason if resolution else None,
                }
            )
        return {"items": items, "has_more": len(rows) > limit, "next_offset": offset + min(len(rows), limit)}

    @app.post("/api/projects/{project_id}/guides/{job_id}/feedback")
    def send_feedback(
        project_id: uuid.UUID, job_id: uuid.UUID, body: FeedbackInput, request: Request, db: DB
    ):
        identity, job = access(project_id, job_id, request, db)

        def save():
            check_current(db, job, body.publication_version, allow_archived=True)
            row = Feedback(
                job_id=job.id, author_id=identity.user.id, device_id=identity.device.id, **body.model_dump()
            )
            db.add(row)
            db.flush()
            # Do not put reader text into source-scoped work logs or push payloads.
            hub.audit(
                db, identity.user.id, "guide.feedback_received", row.id, request.state.request_id, project_id
            )
            return {"id": str(row.id), "state": "open"}

        return mutate(request, db, identity, body, save)

    @app.post("/api/projects/{project_id}/guides/{job_id}/feedback/{feedback_id}/resolutions")
    def resolve(
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        feedback_id: uuid.UUID,
        body: ResolutionInput,
        request: Request,
        db: DB,
    ):
        identity, job = access(project_id, job_id, request, db, "tasks:publish")
        row = db.get(Feedback, feedback_id)
        if not row or row.job_id != job.id:
            raise HubError("GUIDE_FEEDBACK_NOT_ACCESSIBLE", 404)

        def save():
            db.add(
                Resolution(
                    feedback_id=row.id,
                    author_id=identity.user.id,
                    device_id=identity.device.id,
                    **body.model_dump(),
                )
            )
            event(
                hub, db, identity, job, "feedback_resolved", request.state.request_id, feedback_id=str(row.id)
            )
            db.flush()
            return {"id": str(row.id), "state": body.state}

        return mutate(request, db, identity, body, save)
