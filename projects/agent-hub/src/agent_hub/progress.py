"""Append-only project reports. These are checkpoints, not task ownership records."""

from sqlalchemy import select

from .models import ProjectProgress


def view(entry):
    return {
        "sequence": str(entry.sequence),
        "project_id": str(entry.project_id),
        "author_id": str(entry.author_id),
        "device_id": str(entry.device_id),
        "status": entry.status,
        "summary": entry.summary,
        "detail": entry.detail,
        "task_reference": entry.task_reference,
        "git_commit": entry.git_commit,
        "created_at": entry.created_at.isoformat(),
    }


def report(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:write")
    entry = ProjectProgress(
        project_id=project_id, author_id=actor.user.id, device_id=actor.device.id,
        **body.model_dump(mode="python"),
    )
    db.add(entry)
    db.flush()
    hub.audit(db, actor.user.id, "progress.reported", entry.sequence, request_id, project_id)
    return view(entry)


def page(db, project_id, after, limit):
    entries = list(db.scalars(
        select(ProjectProgress)
        .where(ProjectProgress.project_id == project_id, ProjectProgress.sequence > after)
        .order_by(ProjectProgress.sequence).limit(limit)
    ))
    return {"items": [view(entry) for entry in entries],
            "cursor": str(entries[-1].sequence if entries else after), "has_more": len(entries) == limit}
