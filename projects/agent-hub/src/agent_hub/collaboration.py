"""Versioned project occupations and explicitly selected task workflows."""

from sqlalchemy import select

from . import roles
from .models import CodeDelivery, ProjectCollaboration
from .security import HubError

ADMISSIONS = {
    "standard": ["requirements", "design", "feasibility"],
    "compact": ["requirements", "feasibility"],
    "strict": ["requirements", "design", "feasibility"],
}


def current(db, project_id):
    row = db.scalar(
        select(ProjectCollaboration)
        .where(ProjectCollaboration.project_id == project_id)
        .order_by(ProjectCollaboration.version.desc())
        .limit(1)
    )
    content = row.content if row else {"profiles": [], "allow_compact": False}
    payload = {"version": row.version if row else 0, "require_guides": False, **content}
    return {**payload, "content_hash": roles.digest(payload), "grants_permissions": False}


def publish(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    previous = current(db, project_id)
    if previous["version"] != body.expected_version:
        raise HubError("COLLABORATION_VERSION_CONFLICT", 409, current_version=previous["version"])
    old_roles = {p["key"]: p["base_role"] for content in db.scalars(select(ProjectCollaboration.content)
        .where(ProjectCollaboration.project_id == project_id).order_by(ProjectCollaboration.version))
        for p in content["profiles"]}
    if any(p.key in old_roles and old_roles[p.key] != p.base_role for p in body.profiles):
        raise HubError("ROLE_PROFILE_BASE_FROZEN", 409)
    content = body.model_dump(mode="json", exclude={"expected_version", "change_reason"})
    if content["require_guides"] is None:
        content["require_guides"] = previous["require_guides"]
    row = ProjectCollaboration(
        project_id=project_id,
        version=previous["version"] + 1,
        author_id=actor.user.id,
        device_id=actor.device.id,
        content=content,
        content_hash=roles.digest(content),
    )
    db.add(row)
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "collaboration.published",
        row.version,
        request_id,
        project_id,
        reason=body.change_reason,
        content_hash=row.content_hash,
    )
    return current(db, project_id)


def profile(db, project_id, key):
    entry = next((p for p in current(db, project_id)["profiles"] if p["key"] == key and p["enabled"]), None)
    if not entry:
        raise HubError("ROLE_PROFILE_UNAVAILABLE", 409)
    return entry


def task_role(db, task):
    base = roles.role(task.execution_role)
    if not task.role_profile:
        return base
    entry = profile(db, task.project_id, task.role_profile)
    if entry["base_role"] != task.execution_role:
        raise HubError("ROLE_PROFILE_MISMATCH", 409)
    value = {
        **base,
        "profile": entry,
        "name": entry["name"],
        "responsibilities": base["responsibilities"] + entry["responsibilities"],
        "outputs": base["outputs"] + entry["outputs"],
    }
    value.pop("content_hash", None)
    return {**value, "content_hash": roles.digest(value)}


def workflow(db, project_id, name, reason):
    config = current(db, project_id)
    if name == "compact" and not config["allow_compact"]:
        raise HubError("COMPACT_WORKFLOW_DISABLED", 409)
    if name != "standard" and len(reason.strip()) < 10:
        raise HubError("WORKFLOW_REASON_REQUIRED", 422)
    return {
        "name": name,
        "admission": ADMISSIONS[name],
        "post_submission": ["review", "test", "release"],
        "require_verified_delivery": name == "strict",
        "configuration_version": config["version"],
    }


def admission(task):
    return (task.workflow_policy or {}).get("admission", ADMISSIONS["standard"])


def validate_new_task(db, task):
    task_role(db, task)
    if (task.execution_mode == "observed" and current(db, task.project_id)["require_guides"]
            and task.documentation.get("kind", "none") == "none"
            and len(task.documentation.get("reason", "").strip()) < 10):
        raise HubError("GUIDE_PLAN_REQUIRED", 422)
    if task.workflow == "strict" and (task.execution_mode != "observed" or not task.repository_id):
        raise HubError("WORKFLOW_CODE_REPOSITORY_REQUIRED", 422)
    task.workflow_policy = workflow(db, task.project_id, task.workflow, task.workflow_reason)


def change_workflow(hub, db, actor, task, body, request_id):
    from . import tasks

    hub.project(db, actor, task.project_id, "tasks:publish")
    tasks.check_version(task, body.expected_version)
    if task.status not in {"draft", "ready"} or task.active_run_id:
        raise HubError("WORKFLOW_CHANGE_REQUIRES_RELEASE", 409)
    order = {"compact": 0, "standard": 1, "strict": 2}
    if task.lease_epoch and order[body.workflow] <= order[task.workflow]:
        raise HubError("WORKFLOW_DOWNGRADE_FORBIDDEN", 409)
    if body.workflow == "strict" and (task.execution_mode != "observed" or not task.repository_id):
        raise HubError("WORKFLOW_CODE_REPOSITORY_REQUIRED", 422)
    task.workflow_policy = workflow(db, task.project_id, body.workflow, body.reason)
    task.workflow, task.workflow_reason = body.workflow, body.reason.strip()
    task.status, task.admission_step, task.version = "draft", 0, task.version + 1
    hub.audit(
        db,
        actor.user.id,
        "task.workflow_changed",
        task.id,
        request_id,
        task.project_id,
        repository_id=task.repository_id,
        workflow=task.workflow,
        reason=task.workflow_reason,
        workflow_policy=task.workflow_policy,
    )
    tasks.emit(hub, db, actor, task, "task.workflow_changed", request_id)
    return tasks.task_view(task)


def check_release(db, task):
    from .delivery_evidence import check_release as check_external
    check_external(db, task)
    if (task.workflow_policy or {}).get("require_verified_delivery"):
        if (
            task.execution_mode != "observed"
            or not task.repository_id
            or not db.scalar(
                select(CodeDelivery.id).where(
                    CodeDelivery.project_id == task.project_id, CodeDelivery.worklog_id == task.submission_id
                )
            )
        ):
            raise HubError("WORKFLOW_VERIFIED_DELIVERY_REQUIRED", 409)


def endpoint_profiles(db, project_id, keys, base_roles):
    for key in keys:
        if profile(db, project_id, key)["base_role"] not in base_roles:
            raise HubError("AGENT_ROLE_PROFILE_MISMATCH", 422)


def endpoint_matches(task, endpoint):
    return task.execution_role in endpoint.roles and (
        not task.role_profile or task.role_profile in endpoint.role_profiles
    )
