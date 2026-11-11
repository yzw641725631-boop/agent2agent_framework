"""Database-owned workflow limits; only a person can resume a paused cycle."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request
from pydantic import Field, SecretStr
from sqlalchemy import func, select

from . import schemas as S
from .models import TaskWorkflowEvent
from .security import HubError, now, rate_limit, verify_password

STOP_INSTRUCTIONS = (
    "This task requires human resolution. Stop development, Git pushes, submission and gate decisions. "
    "Preserve existing work and report the reason. The current run owner may still save worklogs and "
    "upload permitted code snapshots. Do not uninstall the project or treat this pause as revocation. "
    "Ask an authorized person to open the task in the web console and record a resolution. "
    "Never request, collect or submit their password on their behalf."
)


def view(task):
    value = dict(task.workflow_runtime or {})
    deadline = value.get("deadline_at")
    overdue = value.get("state") == "active" and bool(deadline) and datetime.fromisoformat(deadline) <= now()
    return {
        **value,
        "manual_required": value.get("state") == "manual" or overdue,
        "reason": "stage_timeout" if overdue else value.get("reason"),
        "preservation_only": value.get("state") == "manual" or overdue,
    }


def check(db, task):
    # GET/stream/Git authorization holds a shared project lock. Do not upgrade it.
    # The read view fences a due task immediately; the watchdog persists its event.
    if not db.info.get("workflow_exclusive", False):
        return view(task)
    value = db.scalar(select(func.hub.check_task_workflow(task.project_id, task.id)))
    if value is None:
        raise HubError("TASK_NOT_FOUND", 404)
    if value != task.workflow_runtime:
        db.refresh(task)
    return view(task)


def require_open(db, task):
    control = check(db, task)
    if control["manual_required"]:
        raise HubError(
            "TASK_MANUAL_RESOLUTION_REQUIRED",
            409,
            persist=bool(db.info.get("workflow_exclusive", False)),
            workflow_control=control,
            instructions=STOP_INSTRUCTIONS,
        )
    return control


def history(db, task):
    rows = db.scalars(
        select(TaskWorkflowEvent)
        .where(TaskWorkflowEvent.project_id == task.project_id, TaskWorkflowEvent.task_id == task.id)
        .order_by(TaskWorkflowEvent.created_at.desc(), TaskWorkflowEvent.id.desc())
        .limit(100)
    )
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at.isoformat(),
            "cycle": row.cycle,
            "revision": row.revision,
            "kind": row.kind,
            "reason": row.reason,
            "snapshot": row.snapshot,
            "author_id": str(row.author_id) if row.author_id else None,
            "device_id": str(row.device_id) if row.device_id else None,
            "resolution": row.resolution,
        }
        for row in rows
    ]


class Resume(S.Input):
    expected_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=2000)
    current_password: SecretStr = Field(min_length=1, max_length=256)


def install(app, hub, actor, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]

    @app.post("/api/projects/{project_id}/tasks/{task_id}/workflow-resume")
    def resume(project_id: uuid.UUID, task_id: uuid.UUID, body: Resume, request: Request, db: DB):
        from . import tasks

        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        task = tasks.get_task(db, project_id, task_id)
        rate_limit(security_sessions, hub.secret, f"workflow-resume:{identity.user.id}", 10, 3600)
        if not verify_password(identity.user.password_hash, body.current_password.get_secret_value()):
            raise HubError("INVALID_CREDENTIALS", 401)
        # Passwords and recovery results never enter the generic idempotency cache.
        if not db.scalar(
            select(
                func.hub.resume_task_workflow(
                    project_id, task_id, body.expected_version, body.expected_revision, body.reason
                )
            )
        ):
            raise HubError("WORKFLOW_RESOLUTION_CONFLICT", 409)
        db.refresh(task)
        return {
            "task": tasks.task_view(task),
            "reclaim_required": task.status == "ready",
            "old_run_reactivated": False,
        }
