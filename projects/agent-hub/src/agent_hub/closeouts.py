"""Upload-only recovery after revocation. A closeout is never a normal session."""
import asyncio
import hashlib
import hmac
import tempfile
import uuid
from datetime import timedelta
from types import SimpleNamespace

from fastapi import Depends, Request
from pydantic import Field
from sqlalchemy import select, text

from . import archive_codec, tasks, work_archives, work_policy
from .db import context, coordinate
from .models import (
    CloseoutJob,
    CloseoutRun,
    CloseoutSeal,
    Device,
    Grant,
    ProjectRepository,
    TaskRun,
    User,
    Worklog,
    WorkSnapshot,
)
from .schemas import Input, WorklogInput
from .security import HubError, now, payload_hash, rate_limit


def credential(hub, job_id):
    return "co1_" + hmac.digest(hub.secret.encode(), b"closeout-upload-v1\0" + str(job_id).encode(), "sha256").hex()


def create(hub, db, *, user_id=None, project_id=None, device_id=None, reason, repository_ids=None):
    """Called before removing authorization, under the subject's coordination lock."""
    statements = []
    for model in (Grant, TaskRun):
        statement = select(model.project_id, model.user_id, model.device_id)
        for name, value in (("user_id", user_id), ("project_id", project_id), ("device_id", device_id)):
            if value is not None:
                statement = statement.where(getattr(model, name) == value)
        statements.append(statement)
    statement = statements[0].union(statements[1])
    jobs = []
    for pid, uid, did in db.execute(statement):
        user, device = db.get(User, uid), db.get(Device, did)
        grant_ids = [str(value) for value in db.scalars(select(Grant.id).where(
            Grant.project_id == pid, Grant.user_id == uid, Grant.device_id == did))]
        versions = {str(repo.id): repo.version for repo in db.scalars(select(ProjectRepository).where(
            ProjectRepository.project_id == pid, ProjectRepository.id.in_(repository_ids or [])))}
        job = CloseoutJob(id=uuid.uuid4(), created_at=now(), project_id=pid, user_id=uid, device_id=did,
            reason=reason, scope={"repository_ids": repository_ids, "grant_ids": grant_ids,
                "user_epoch": user.auth_epoch, "device_epoch": device.epoch}, expires_at=now() + timedelta(days=30))
        job.scope["repository_versions"] = versions
        job.token_digest = hub.hash(credential(hub, job.id))
        db.add(job)
        db.flush()
        db.execute(text("SELECT hub.closeout_freeze(:id)"), {"id": job.id})
        jobs.append(str(job.id))
    return jobs


def view(job):
    return {"id": str(job.id), "project_id": str(job.project_id), "user_id": str(job.user_id),
        "device_id": str(job.device_id), "reason": job.reason, "scope": job.scope, "state": job.state,
        "created_at": job.created_at.isoformat(), "expires_at": job.expires_at.isoformat(),
        "normal_operations_allowed": False, "required_action": "upload_work_then_uninstall",
        "server_upload_confirmed": job.seal_id is not None,
        "cleanup_report": job.cleanup_report, "cleanup_verification": "client_report_only",
        "expired": job.expires_at <= now()}


def lookup(db, thumbprint, job_id=None, *, after=None):
    statement = select(CloseoutJob).from_statement(text(
        "SELECT * FROM hub.closeout_discover(:thumb)" + (" WHERE id=:id" if job_id else
        " WHERE (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT 101")))
    return list(db.scalars(statement, {"thumb": thumbprint, "id": job_id, "after": str(after) if after else None}))


def authenticate(hub, db, thumbprint, value, job_id, *, writable=False, resources=()):
    rows = lookup(db, thumbprint, job_id)
    if not rows or not hmac.compare_digest(value, credential(hub, job_id)):
        raise HubError("CLOSEOUT_NOT_ACCESSIBLE", 404)
    job = rows[0]
    user_id, device_id, project_id = job.user_id, job.device_id, job.project_id
    coordinate(db, user_ids=[user_id], project_ids=[project_id], resources=["closeout:" + str(job_id), *resources])
    db.expire_all()
    context(db, user_id, device_id)
    for key, val in (("hub.closeout_id", str(job_id)), ("hub.closeout_digest", hub.hash(value))):
        db.execute(text("SELECT set_config(:key,:value,true)"), {"key": key, "value": val})
    job = db.get(CloseoutJob, job_id, populate_existing=True)
    if not job:
        raise HubError("CLOSEOUT_NOT_ACCESSIBLE", 404)
    if writable and (job.expires_at <= now() or job.state == "complete"):
        raise HubError("CLOSEOUT_EXPIRED" if job.expires_at <= now() else "CLOSEOUT_ALREADY_COMPLETE", 409,
            normal_operations_allowed=False, required_action="preserve_work_contact_superadmin")
    return job


class Checkpoint(WorklogInput):
    operation_id: uuid.UUID
    task_id: uuid.UUID


class SealInput(Input):
    worklog_ids: list[uuid.UUID] = Field(max_length=1000)


class CleanupInput(Input):
    seal_id: uuid.UUID
    receipt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: str = Field(pattern=r"^(complete|conflicts)$")
    removed_files: int = Field(ge=0, le=10000)
    conflicts: int = Field(ge=0, le=10000)
    retained_shared_components: bool = True


class ExtendInput(Input):
    days: int = Field(ge=1, le=30)


def record(hub, db, job, body):
    frozen = db.get(CloseoutRun, (job.id, body.run_id))
    if not frozen or frozen.task_id != body.task_id or frozen.lease_epoch != body.lease_epoch:
        raise HubError("CLOSEOUT_RUN_NOT_AUTHORIZED", 403)
    payload = frozen.payload
    if body.context_id and body.context_id != frozen.context_id:
        payload = db.scalar(text("SELECT hub.closeout_context(:cid,:rid,:wid)"),
            {"cid": job.id, "rid": body.run_id, "wid": body.context_id})
    if not payload or (not body.context_id and frozen.context_id) or not body.change_evidence:
        raise HubError("CLOSEOUT_CONTEXT_MISMATCH", 409)
    evidence = body.change_evidence.model_dump(mode="json")
    paths = [file["path"] for file in evidence["files"]]
    work_policy.check_paths(paths, payload["task"]["scope"], payload["policy"])
    if sorted(paths) != sorted(body.changed_paths) or body.git_commit != evidence["head_commit"]:
        raise HubError("WORK_CHANGE_PATHS_MISMATCH", 422)
    log_id = uuid.uuid5(job.id, str(body.operation_id))
    values = body.model_dump(mode="python", exclude={"task_id", "operation_id", "change_evidence"})
    existing = db.get(Worklog, log_id)
    if existing:
        # The creation timestamp/id are server-owned; all client-controlled fields must match.
        if any(getattr(existing, key) != value for key, value in values.items()) or existing.change_evidence != evidence:
            raise HubError("IDEMPOTENCY_PAYLOAD_CHANGED", 409)
        return tasks.log_view(existing)
    log = Worklog(id=log_id, created_at=now(), project_id=job.project_id, task_id=body.task_id,
        author_id=job.user_id, device_id=job.device_id, closeout_id=job.id, change_evidence=evidence,
        evidence_hash=payload_hash(evidence), **values)
    log.receipt_hash = payload_hash(tasks.log_payload(log) | {"closeout_id": str(job.id)})
    db.add(log)
    job.seal_id, job.state = None, "pending"
    db.flush()
    hub.audit(db, job.user_id, "closeout.worklog_saved", log.id, "closeout-upload", job.project_id,
        repository_id=frozen.repository_id, closeout_id=str(job.id))
    return tasks.log_view(log)


def seal(hub, db, job, body):
    ids = set(body.worklog_ids)
    logs = list(db.scalars(select(Worklog).where(Worklog.closeout_id == job.id)))
    if len(ids) != len(body.worklog_ids) or ids != {row.id for row in logs}:
        raise HubError("CLOSEOUT_INVENTORY_MISMATCH", 409)
    if not ids and db.scalar(select(CloseoutRun.run_id).where(CloseoutRun.job_id == job.id).limit(1)):
        raise HubError("CLOSEOUT_CODE_BACKUP_REQUIRED", 409)
    inventory = []
    for log in sorted(logs, key=lambda row: str(row.id)):
        snapshot = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == log.id))
        if not snapshot:
            raise HubError("CLOSEOUT_CODE_BACKUP_REQUIRED", 409)
        work_archives.ArtifactStore(hub.settings).read(snapshot)
        inventory.append({"worklog_id": str(log.id), "evidence_hash": log.evidence_hash,
            "snapshot_id": str(snapshot.id), "receipt_hash": snapshot.receipt_hash})
    previous = db.get(CloseoutSeal, job.seal_id) if job.seal_id else None
    if previous and previous.inventory == inventory:
        result = previous
    else:
        result = CloseoutSeal(id=uuid.uuid4(), created_at=now(), job_id=job.id, inventory=inventory)
        result.receipt_hash = payload_hash({"id": str(result.id), "job_id": str(job.id), "inventory": inventory})
        db.add(result)
        db.flush()
        job.seal_id, job.state = result.id, "uploaded"
    hub.audit(db, job.user_id, "closeout.upload_confirmed", result.id, "closeout-seal", job.project_id,
        checkpoints=len(inventory), closeout_id=str(job.id))
    return {"id": str(result.id), "job_id": str(job.id), "receipt_hash": result.receipt_hash,
        "checkpoints": len(inventory), "server_code_backup": True, "disaster_redundant": False,
        "inventory_hash": payload_hash(inventory), "required_action": "uninstall_owned_project_framework"}


def install(app, hub, actor, token, proof, db_dependency):
    database = Depends(db_dependency, scope="function")

    @app.post("/api/closeouts/discover")
    def discover(request: Request, after: uuid.UUID | None = None, db=database):
        _, thumb = proof(request)
        rate_limit(app.state.security_sessions, hub.secret, "closeout-discover:" + thumb, 30, 60)
        rows = lookup(db, thumb, after=after)
        return {"items": [view(row) | {"upload_token": credential(hub, row.id)} for row in rows[:100]],
            "next_cursor": str(rows[99].id) if len(rows) > 100 else None}

    def identity(request, db, job_id, *, writable=False):
        value = token(request)
        _, thumb = proof(request, value)
        return authenticate(hub, db, thumb, value, job_id, writable=writable), thumb, value

    @app.post("/api/closeouts/{job_id}/checkpoints")
    def checkpoint(job_id: uuid.UUID, body: Checkpoint, request: Request, db=database):
        job, _, _ = identity(request, db, job_id, writable=True)
        return record(hub, db, job, body)

    @app.post("/api/closeouts/{job_id}/checkpoints/{log_id}/snapshot")
    async def upload(job_id: uuid.UUID, log_id: uuid.UUID, request: Request, db=database):
        job, thumb, value = identity(request, db, job_id, writable=True)
        if request.headers.get("content-type", "").split(";")[0] != "application/gzip":
            raise HubError("WORK_ARCHIVE_INVALID", 415)
        rate_limit(app.state.security_sessions, hub.secret, "closeout-upload:" + str(job.device_id), 30, 60)
        db.commit()
        if not work_archives.SLOTS.acquire(blocking=False):
            raise HubError("SERVICE_BUSY", 503)
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
                size = 0
                try:
                    async with asyncio.timeout(120):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > archive_codec.MAX_ARCHIVE:
                                raise HubError("WORK_ARCHIVE_LIMIT", 413)
                            spool.write(chunk)
                except TimeoutError as exc:
                    raise HubError("WORK_ARCHIVE_UPLOAD_TIMEOUT", 408) from exc
                spool.seek(0)
                data = spool.read()
            if hashlib.sha256(data).hexdigest() != request.headers.get("X-Content-SHA256"):
                raise HubError("WORK_ARCHIVE_HASH_MISMATCH", 422)
            job = authenticate(hub, db, thumb, value, job_id, writable=True, resources=["work-archives"])
            log = db.scalar(select(Worklog).where(Worklog.id == log_id, Worklog.closeout_id == job.id))
            if not log:
                raise HubError("WORKLOG_NOT_ACCESSIBLE", 404)
            principal = SimpleNamespace(user=SimpleNamespace(id=job.user_id), device=SimpleNamespace(id=job.device_id))
            return await asyncio.to_thread(work_archives.save, hub, db, principal, log, data, closeout=True)
        finally:
            work_archives.SLOTS.release()

    @app.post("/api/closeouts/{job_id}/seal")
    def confirm_upload(job_id: uuid.UUID, body: SealInput, request: Request, db=database):
        job, _, _ = identity(request, db, job_id, writable=True)
        return seal(hub, db, job, body)

    @app.post("/api/closeouts/{job_id}/cleanup-report")
    def cleanup_report(job_id: uuid.UUID, body: CleanupInput, request: Request, db=database):
        job, _, _ = identity(request, db, job_id)
        receipt = db.get(CloseoutSeal, job.seal_id) if job.seal_id else None
        if not receipt or receipt.id != body.seal_id or receipt.receipt_hash != body.receipt_hash:
            raise HubError("CLOSEOUT_UPLOAD_CONFIRMATION_REQUIRED", 409)
        report = body.model_dump(mode="json")
        if body.result == "complete" and body.conflicts:
            raise HubError("CLOSEOUT_CLEANUP_CONFLICT", 409)
        if job.state == "complete" and report != job.cleanup_report:
            raise HubError("CLOSEOUT_ALREADY_COMPLETE", 409)
        job.cleanup_report, job.state = report, body.result
        hub.audit(db, job.user_id, "closeout.cleanup_reported", job.id, request.state.request_id, job.project_id,
            result=body.result, conflicts=body.conflicts, verification="client_report_only")
        return {"state": job.state, "cleanup_verification": "client_report_only", "normal_operations_allowed": False}

    @app.get("/api/projects/{project_id}/closeouts")
    def list_jobs(project_id: uuid.UUID, request: Request, db=database):
        who = actor(request, db)
        hub.manage_members(db, who, project_id)
        rows = db.scalars(select(CloseoutJob).where(CloseoutJob.project_id == project_id)
            .order_by(CloseoutJob.created_at.desc()).limit(100))
        return {"items": [view(row) for row in rows]}

    @app.post("/api/admin/closeouts/{job_id}/extend")
    def extend(job_id: uuid.UUID, body: ExtendInput, request: Request, db=database):
        from .service import require_root
        who = actor(request, db)
        require_root(who)
        job = db.get(CloseoutJob, job_id)
        if not job or job.state == "complete":
            raise HubError("CLOSEOUT_NOT_ACCESSIBLE", 404)
        coordinate(db, user_ids=[job.user_id], project_ids=[job.project_id])
        job.expires_at = now() + timedelta(days=body.days)
        hub.audit(db, who.user.id, "closeout.extended", job.id, request.state.request_id, job.project_id, days=body.days)
        return view(job)
