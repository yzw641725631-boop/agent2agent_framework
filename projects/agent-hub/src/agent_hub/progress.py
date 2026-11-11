"""Append-only project reports. These are checkpoints, not task ownership records."""

from sqlalchemy import select

from .models import ProjectProgress
from .security import HubError


def view(entry):
    return {
        "sequence": str(entry.sequence),
        "project_id": str(entry.project_id),
        "repository_id": str(entry.repository_id) if entry.repository_id else None,
        "author_id": str(entry.author_id) if entry.author_id else None,
        "device_id": str(entry.device_id) if entry.device_id else None,
        "source": "person" if entry.author_id else "system",
        "status": entry.status,
        "summary": entry.summary,
        "detail": entry.detail,
        "task_reference": entry.task_reference,
        "git_commit": entry.git_commit,
        "kind": entry.kind,
        "task_id": str(entry.task_id) if entry.task_id else None,
        "run_id": str(entry.run_id) if entry.run_id else None,
        "worklog_id": str(entry.worklog_id) if entry.worklog_id else None,
        "created_at": entry.created_at.isoformat(),
    }


def report(hub, db, actor, project_id, body, request_id):
    project = hub.project(db, actor, project_id, "tasks:write")
    if body.repository_id:
        from .repositories import require
        require(db, project_id, body.repository_id, "code:read")
    elif project.repository_mode == "scoped":
        raise HubError("PROGRESS_REPOSITORY_REQUIRED", 422)
    entry = ProjectProgress(
        project_id=project_id, author_id=actor.user.id, device_id=actor.device.id,
        **body.model_dump(mode="python"),
    )
    db.add(entry)
    db.flush()
    hub.audit(db, actor.user.id, "progress.reported", entry.sequence, request_id, project_id,
              repository_id=entry.repository_id)
    return view(entry)


def page(db, project_id, after, limit):
    entries = list(db.scalars(
        select(ProjectProgress)
        .where(ProjectProgress.project_id == project_id, ProjectProgress.sequence > after)
        .order_by(ProjectProgress.sequence).limit(limit)
    ))
    return {"items": [view(entry) for entry in entries],
            "cursor": str(entries[-1].sequence if entries else after), "has_more": len(entries) == limit}


def recent(db, project_id, before, limit):
    query = select(ProjectProgress).where(ProjectProgress.project_id == project_id)
    if before:
        query = query.where(ProjectProgress.sequence < before)
    entries = list(db.scalars(query.order_by(ProjectProgress.sequence.desc()).limit(limit)))
    return {"items": [view(entry) for entry in entries],
            "before": str(entries[-1].sequence if entries else before), "has_more": len(entries) == limit}
