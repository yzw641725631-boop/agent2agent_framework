"""Project-serialized task aggregates. Narrative progress never changes task state."""

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select, text

from . import collaboration, repositories, roles, work_policy
from .models import (
    CodeDelivery,
    ProjectProgress,
    Task,
    TaskDecision,
    TaskDependency,
    TaskRun,
    Worklog,
    WorkSnapshot,
)
from .security import HubError, now

LEASE_SECONDS = 600
ADMISSION = ("requirements", "design", "feasibility")


def task_view(t):
    return {"id": str(t.id), "project_id": str(t.project_id), "parent_id": str(t.parent_id) if t.parent_id else None,
            "repository_id": str(t.repository_id) if t.repository_id else None,
            "title": t.title, "description": t.description, "acceptance": t.acceptance, "scope": t.scope,
            "execution_mode": t.execution_mode, "execution_role": t.execution_role,
            "role_profile": t.role_profile, "workflow": t.workflow,
            "workflow_reason": t.workflow_reason, "workflow_policy": t.workflow_policy,
            "documentation": t.documentation,
            "status": t.status, "admission_step": t.admission_step, "version": t.version,
            "lease_epoch": t.lease_epoch, "active_run_id": str(t.active_run_id) if t.active_run_id else None,
            "lease_until": t.lease_until.isoformat() if t.lease_until else None,
            "submission_id": str(t.submission_id) if t.submission_id else None,
            "created_at": t.created_at.isoformat()}


def run_view(r):
    return {"id": str(r.id), "user_id": str(r.user_id), "device_id": str(r.device_id),
            "instance_id": str(r.instance_id), "harness": r.harness, "role": r.role,
            "lease_epoch": r.lease_epoch, "state": r.state, "created_at": r.created_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None}


def log_payload(w):
    value = {"id": str(w.id), "project_id": str(w.project_id), "task_id": str(w.task_id),
            "run_id": str(w.run_id), "author_id": str(w.author_id), "device_id": str(w.device_id),
            "lease_epoch": w.lease_epoch, "summary": w.summary, "notes": w.notes,
            "changed_paths": w.changed_paths, "tests": w.tests, "next_steps": w.next_steps,
            "outcome": w.outcome, "git_commit": w.git_commit, "created_at": w.created_at.isoformat()}
    if w.context_id:
        value.update(context_id=str(w.context_id), change_evidence=w.change_evidence, evidence_hash=w.evidence_hash)
    if w.closeout_id:
        value.update(closeout_id=str(w.closeout_id), record_kind="revocation_handoff")
    if w.turn_id:
        value["turn_id"] = str(w.turn_id)
    return value


def log_view(w, snapshot=None, delivery=None):
    from .work_archives import delivery_view, snapshot_view
    return {**log_payload(w), "receipt_hash": w.receipt_hash, "code_backup": snapshot is not None,
            "snapshot": snapshot_view(snapshot) if snapshot else None,
            "git_commit_verified": delivery is not None, "delivery": delivery_view(delivery) if delivery else None}


def decision_view(d):
    return {"id": str(d.id), "gate": d.gate, "approved": d.approved, "author_id": str(d.author_id),
            "device_id": str(d.device_id), "run_id": str(d.run_id) if d.run_id else None,
            "worklog_id": str(d.worklog_id) if d.worklog_id else None, "evidence": d.evidence,
            "override_reason": d.override_reason, "task_version": d.task_version,
            "created_at": d.created_at.isoformat()}


def get_task(db, project_id, task_id):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.project_id == project_id))
    if not task:
        raise HubError("TASK_NOT_FOUND", 404)
    return task


def dependencies_satisfied(db, task):
    return db.scalar(select(func.hub.task_dependencies_satisfied(task.project_id, task.id)))


def dependency_view(db, task):
    rows = db.scalars(select(Task).join(TaskDependency, TaskDependency.depends_on_id == Task.id).where(
        TaskDependency.project_id == task.project_id, TaskDependency.task_id == task.id).order_by(Task.id))
    return {"dependencies_satisfied": dependencies_satisfied(db, task),
            "dependencies": [{"id": str(t.id), "title": t.title, "status": t.status,
                              "repository_id": str(t.repository_id) if t.repository_id else None} for t in rows],
            "has_restricted_dependencies": db.scalar(select(
                func.hub.task_has_restricted_dependencies(task.project_id, task.id)))}


def scope_conflicts(db, task):
    if task.execution_mode != "observed":
        return []
    statement = select(Task).where(Task.project_id == task.project_id, Task.id != task.id,
        Task.execution_mode == "observed", Task.repository_id == task.repository_id,
        ((Task.status == "in_progress") & (Task.lease_until > now())) |
        Task.status.in_(["review", "testing", "release_ready"]))
    paths = [p.lower() for p in task.scope["paths"]]
    conflicts = []
    for other in db.scalars(statement.order_by(Task.id)):
        if not task.repository_id and other.scope["repository"].lower() != task.scope["repository"].lower():
            continue
        overlaps = [p for p in other.scope["paths"] if any(
            p.lower() == root or p.lower().startswith(root + "/") or root.startswith(p.lower() + "/")
            for root in paths)]
        if overlaps:
            conflicts.append({"task_id": str(other.id), "status": other.status, "paths": overlaps})
            if len(conflicts) == 10:
                break
    return conflicts


def set_dependencies(db, task, dependency_ids):
    ids = sorted(set(dependency_ids))
    if len(ids) != len(dependency_ids):
        raise HubError("TASK_DEPENDENCY_DUPLICATE", 422)
    # Both endpoints must be visible to the publisher. A hidden edge cannot be removed by omission.
    if db.scalar(select(func.hub.task_has_restricted_dependencies(task.project_id, task.id))):
        raise HubError("TASK_DEPENDENCIES_RESTRICTED", 403)
    visible = set(db.scalars(select(Task.id).where(Task.project_id == task.project_id, Task.id.in_(ids))))
    if visible != set(ids):
        raise HubError("TASK_DEPENDENCY_NOT_ACCESSIBLE", 404)
    if db.scalar(text("SELECT hub.task_dependency_cycle(:pid,:tid,CAST(:deps AS uuid[]))"),
                 {"pid": task.project_id, "tid": task.id, "deps": ids}):
        raise HubError("TASK_DEPENDENCY_CYCLE", 409)
    db.execute(delete(TaskDependency).where(TaskDependency.project_id == task.project_id,
                                           TaskDependency.task_id == task.id))
    db.add_all(TaskDependency(project_id=task.project_id, task_id=task.id, depends_on_id=value) for value in ids)
    db.flush()


def update_dependencies(hub, db, actor, project_id, task_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    task = get_task(db, project_id, task_id)
    check_version(task, body.expected_version)
    if task.lease_epoch or task.status not in {"draft", "ready"}:
        raise HubError("TASK_DEPENDENCIES_FROZEN", 409)
    set_dependencies(db, task, body.depends_on)
    # Changed scheduling assumptions require a new admission, preserving all old decisions.
    task.status, task.admission_step, task.version = "draft", 0, task.version + 1
    hub.audit(db, actor.user.id, "task.dependency_reason", task.id, request_id, task.project_id,
              repository_id=task.repository_id, reason=body.reason)
    db.flush()
    emit(hub, db, actor, task, "task.dependencies_changed", request_id)
    # Idempotency stores no cross-repository titles or dependency IDs from this response.
    return task_view(task)


def page(db, project_id, offset, limit, *, repository_id=None):
    statement = select(Task).where(Task.project_id == project_id)
    if repository_id:
        statement = statement.where(Task.repository_id == repository_id)
    rows = list(db.scalars(statement
                          .order_by(Task.created_at.desc(), Task.id).offset(offset).limit(limit)))
    states = dict(db.execute(select(Task.id, func.hub.task_dependencies_satisfied(Task.project_id, Task.id))
                             .where(Task.id.in_([t.id for t in rows]))).all()) if rows else {}
    return {"items": [{**task_view(t), "dependencies_satisfied": states[t.id]} for t in rows],
            "has_more": len(rows) == limit, "next_offset": offset + len(rows)}


def detail(db, project_id, task_id):
    task = get_task(db, project_id, task_id)
    runs = db.scalars(select(TaskRun).where(TaskRun.task_id == task_id, TaskRun.project_id == project_id)
                      .order_by(TaskRun.created_at.desc(), TaskRun.id).limit(100))
    decisions = db.scalars(select(TaskDecision).where(TaskDecision.task_id == task_id, TaskDecision.project_id == project_id)
                          .order_by(TaskDecision.created_at.desc(), TaskDecision.id).limit(100))
    conflicts = scope_conflicts(db, task)
    dep = dependency_view(db, task)
    return {"task": task_view(task), "coordination": {**dep, "scope_conflicts": conflicts,
                "claimable": task.status in {"ready", "in_progress"} and dep["dependencies_satisfied"]
                    and not conflicts and (not task.lease_until or task.lease_until <= now())},
            "runs": [run_view(r) for r in runs],
            "decisions": [decision_view(d) for d in decisions]}


def emit(hub, db, actor, task, kind, request_id, *, run_id=None, worklog_id=None):
    # Full notes and gate evidence stay behind logs:read; the shared stream has only state.
    label = {"task.created": "任务已创建", "task.claimed": "任务已认领", "task.released": "认领已释放",
             "task.submitted": "任务待评审", "worklog.recorded": "工作日志已保存", "task.gate_decided": "流程已推进",
             "work.snapshot_saved": "代码检查点已备份", "code.delivery_verified": "代码交付已核验",
             "task.dependencies_changed": "任务前置依赖已更新", "task.workflow_changed": "任务流程已调整"}[kind]
    event = ProjectProgress(project_id=task.project_id, author_id=actor.user.id, device_id=actor.device.id,
                            repository_id=task.repository_id,
                            status="completed" if task.status == "completed" else "checkpoint",
                            summary=f"{label} · {task.title}"[:300], detail="", task_reference=str(task.id),
                            kind=kind, task_id=task.id, run_id=run_id, worklog_id=worklog_id)
    db.add(event)
    hub.audit(db, actor.user.id, kind, task.id, request_id, task.project_id,
              repository_id=task.repository_id,
              task_version=task.version, run_id=str(run_id) if run_id else None,
              worklog_id=str(worklog_id) if worklog_id else None)


def create(hub, db, actor, project_id, body, request_id):
    project = hub.project(db, actor, project_id, "tasks:publish")
    if body.repository_id:
        repo = repositories.require(db, project_id, body.repository_id)
        if body.scope.repository != repo.slug or body.scope.platform != repo.platform:
            raise HubError("TASK_REPOSITORY_MISMATCH", 422)
    elif project.repository_mode == "scoped":
        raise HubError("TASK_REPOSITORY_REQUIRED", 422)
    if body.parent_id:
        parent = get_task(db, project_id, body.parent_id)
        if parent.status == "completed":
            raise HubError("TASK_PARENT_COMPLETED", 409)
    task = Task(project_id=project_id, created_by=actor.user.id,
                **body.model_dump(mode="python", exclude={"depends_on"}))
    collaboration.validate_new_task(db, task)
    db.add(task)
    db.flush()
    set_dependencies(db, task, body.depends_on)
    emit(hub, db, actor, task, "task.created", request_id)
    return task_view(task)


def claim(hub, db, actor, project_id, task_id, body, request_id):
    from .insights import require_budget
    hub.project(db, actor, project_id, "tasks:claim")
    hub.project(db, actor, project_id, "tasks:write")
    task = get_task(db, project_id, task_id)
    require_budget(db, project_id)
    if task.repository_id:
        repositories.require(db, project_id, task.repository_id, "code:write")
    if task.status not in {"ready", "in_progress"}:
        raise HubError("TASK_NOT_CLAIMABLE", 409, task_status=task.status)
    collaboration.task_role(db, task)
    if not dependencies_satisfied(db, task):
        raise HubError("TASK_DEPENDENCIES_INCOMPLETE", 409)
    conflicts = scope_conflicts(db, task)
    if conflicts:
        raise HubError("TASK_SCOPE_CONFLICT", 409, conflicts=conflicts)
    if task.active_run_id:
        if task.lease_until > now():
            raise HubError("TASK_ALREADY_CLAIMED", 409, run_id=str(task.active_run_id),
                           lease_until=task.lease_until.isoformat())
        old = db.get(TaskRun, task.active_run_id)
        old.state, old.ended_at = "expired", now()
        db.flush()  # Release the partial unique index before the next primary writer.
    task.lease_epoch += 1
    run = TaskRun(id=uuid.uuid4(), project_id=project_id, task_id=task_id, user_id=actor.user.id,
                  device_id=actor.device.id, instance_id=body.instance_id, harness=body.harness,
                  role=task.execution_role, lease_epoch=task.lease_epoch)
    db.add(run)
    task.active_run_id, task.lease_until = run.id, now() + timedelta(seconds=LEASE_SECONDS)
    task.status, task.submission_id, task.version = "in_progress", None, task.version + 1
    db.flush()
    emit(hub, db, actor, task, "task.claimed", request_id, run_id=run.id)
    return {"task": task_view(task), "run": run_view(run), "heartbeat_seconds": 120}


def live_run(db, actor, project_id, task_id, body):
    task = get_task(db, project_id, task_id)
    if task.repository_id:
        repositories.require(db, project_id, task.repository_id, "code:write")
    if task.active_run_id != body.run_id or task.lease_epoch != body.lease_epoch or task.status != "in_progress":
        raise HubError("TASK_LEASE_STALE", 409)
    run = db.get(TaskRun, body.run_id)
    if not run or run.user_id != actor.user.id or run.device_id != actor.device.id or run.state != "active":
        raise HubError("TASK_LEASE_NOT_OWNED", 403)
    if not task.lease_until or task.lease_until <= now():
        raise HubError("TASK_LEASE_EXPIRED", 409)
    return task, run


def heartbeat(hub, db, actor, project_id, task_id, body):
    hub.project(db, actor, project_id, "tasks:claim")
    task, _ = live_run(db, actor, project_id, task_id, body)
    task.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
    return {"task": task_view(task), "heartbeat_seconds": 120}


def release(hub, db, actor, project_id, task_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:claim")
    task, run = live_run(db, actor, project_id, task_id, body)
    run.state, run.ended_at = "released", now()
    task.active_run_id, task.lease_until, task.status = None, None, "ready"
    task.version += 1
    hub.audit(db, actor.user.id, "task.release_reason", task_id, request_id, project_id,
              repository_id=task.repository_id, reason=body.reason)
    emit(hub, db, actor, task, "task.released", request_id, run_id=run.id)
    return task_view(task)


def write_log(hub, db, actor, project_id, task_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:write")
    task, run = live_run(db, actor, project_id, task_id, body)
    evidence_value = evidence_hash = None
    if task.execution_mode == "observed":
        evidence_value, evidence_hash = work_policy.evidence(db, actor, task, run, body)
    elif body.context_id or body.change_evidence:
        raise HubError("WORK_EVIDENCE_MODE_MISMATCH", 422)
    for path in body.changed_paths:
        if task.execution_mode != "observed" and not any(path == root or path.startswith(root + "/") for root in task.scope["paths"]):
            raise HubError("TASK_PATH_OUT_OF_SCOPE", 422, path=path)
    log = Worklog(id=uuid.uuid4(), created_at=now(), project_id=project_id, task_id=task_id,
                  author_id=actor.user.id, device_id=actor.device.id,
                  **body.model_dump(mode="python", exclude={"change_evidence"}),
                  change_evidence=evidence_value, evidence_hash=evidence_hash)
    canonical = json.dumps(log_payload(log), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    log.receipt_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    db.add(log)
    db.flush()
    emit(hub, db, actor, task, "worklog.recorded", request_id, run_id=run.id, worklog_id=log.id)
    return log_view(log)


def logs(db, project_id, task_id, offset, limit):
    get_task(db, project_id, task_id)
    rows = list(db.scalars(select(Worklog).where(Worklog.project_id == project_id, Worklog.task_id == task_id)
                          .order_by(Worklog.created_at.desc(), Worklog.id).offset(offset).limit(limit)))
    ids = [row.id for row in rows]
    snapshots = {row.worklog_id: row for row in db.scalars(select(WorkSnapshot).where(WorkSnapshot.worklog_id.in_(ids)))}
    deliveries = {row.worklog_id: row for row in db.scalars(select(CodeDelivery).where(CodeDelivery.worklog_id.in_(ids)))}
    return {"items": [log_view(w, snapshots.get(w.id), deliveries.get(w.id)) for w in rows],
            "has_more": len(rows) == limit, "next_offset": offset + len(rows)}


def log_index(db, project_id, task_id, offset, limit):
    result = logs(db, project_id, task_id, offset, limit)
    result["items"] = [{key: row[key] for key in ("id", "task_id", "author_id", "summary", "outcome", "created_at", "receipt_hash")}
                       | {"evidence_hash": row.get("evidence_hash")} for row in result["items"]]
    return result


def read_log(db, project_id, task_id, log_id):
    get_task(db, project_id, task_id)
    row = db.scalar(select(Worklog).where(Worklog.project_id == project_id, Worklog.task_id == task_id, Worklog.id == log_id))
    if not row:
        raise HubError("WORKLOG_NOT_ACCESSIBLE", 404)
    snapshot = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == row.id))
    delivery = db.scalar(select(CodeDelivery).where(CodeDelivery.worklog_id == row.id))
    return log_view(row, snapshot, delivery)


def check_version(task, version):
    if task.version != version:
        raise HubError("TASK_VERSION_CONFLICT", 409, current_version=task.version)


def submit(hub, db, actor, project_id, task_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:write")
    task, run = live_run(db, actor, project_id, task_id, body)
    check_version(task, body.expected_version)
    latest = db.scalar(select(Worklog).where(Worklog.run_id == run.id, Worklog.project_id == project_id)
                       .order_by(Worklog.created_at.desc(), Worklog.id).limit(1))
    if not latest or latest.id != body.worklog_id or latest.outcome != "checkpoint":
        raise HubError("TASK_LATEST_CHECKPOINT_REQUIRED", 409)
    if task.execution_mode == "observed":
        work_policy.require_context(db, actor, task, run, latest.context_id)
        if not latest.evidence_hash or latest.evidence_hash != body.evidence_hash:
            raise HubError("WORK_EVIDENCE_CHANGED", 409)
        if task.repository_id:
            snapshot = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == latest.id,
                                                            WorkSnapshot.evidence_hash == latest.evidence_hash))
            if not snapshot:
                raise HubError("WORK_REMOTE_BACKUP_REQUIRED", 409)
            # Detect a missing/damaged artifact before ending the developer's lease.
            from .work_archives import ArtifactStore
            ArtifactStore(hub.settings).read(snapshot)
    task.submission_id, task.status, task.version = latest.id, "review", task.version + 1
    task.active_run_id, task.lease_until = None, None
    run.state, run.ended_at = "submitted", now()
    emit(hub, db, actor, task, "task.submitted", request_id, run_id=run.id, worklog_id=latest.id)
    result = task_view(task)
    if task.documentation.get("kind", "none") != "none":
        from .guides import GuideJob
        from .guides import view as guide_view
        db.flush()  # Queue trigger freezes jobs before returning instructions to this same conversation.
        jobs = db.scalars(select(GuideJob).where(GuideJob.task_id == task.id,
            GuideJob.target_user_id == actor.user.id, GuideJob.target_device_id == actor.device.id,
            GuideJob.instance_id == run.instance_id, GuideJob.state.in_(["queued", "running"])))
        result["guide_generation"] = {"items": [guide_view(db, job) for job in jobs],
            "instructions": "Development lease ended. Continue in this same instance: claim each queued guide, "
            "read its frozen input and submit an accurate guide through MCP. Do not edit code or claim successful "
            "verification without evidence. If blocked, leave a durable reason for reassignment. "
            "Other contributors have independent guide jobs; never claim theirs."}
    return result


def decide(hub, db, actor, project_id, task_id, body, request_id):
    cap = {"review": "tasks:review", "test": "tasks:test"}.get(body.gate, "tasks:publish")
    hub.project(db, actor, project_id, cap)
    task = get_task(db, project_id, task_id)
    check_version(task, body.expected_version)
    run_id = None
    if body.gate in ADMISSION:
        admission = collaboration.admission(task)
        if task.status != "draft" or task.admission_step >= len(admission) or body.gate != admission[task.admission_step]:
            raise HubError("TASK_GATE_ORDER_INVALID", 409)
        if body.worklog_id or body.override_reason:
            raise HubError("TASK_GATE_INPUT_INVALID", 422)
        if body.approved:
            task.admission_step += 1
            if task.admission_step == len(admission):
                task.status = "ready"
    else:
        hub.project(db, actor, project_id, "logs:read")
        expected_state = {"review": "review", "test": "testing", "release": "release_ready"}[body.gate]
        if task.status != expected_state or not task.submission_id or body.worklog_id != task.submission_id:
            raise HubError("TASK_GATE_ORDER_INVALID", 409)
        worklog = db.get(Worklog, task.submission_id)
        if worklog.author_id == actor.user.id and body.gate in {"review", "test"}:
            if actor.user.platform_role != "superadmin" or len(body.override_reason.strip()) < 10:
                raise HubError("TASK_INDEPENDENT_REVIEW_REQUIRED", 403)
        elif body.override_reason:
            raise HubError("TASK_OVERRIDE_NOT_APPLICABLE", 422)
        if body.approved and body.gate == "release":
            collaboration.check_release(db, task)
            from . import guides
            guides.check_release(db, task)
            if not db.scalar(select(func.hub.task_children_completed(project_id, task_id))):
                raise HubError("TASK_CHILDREN_INCOMPLETE", 409)
        task.status = ({"review": "testing", "test": "release_ready", "release": "completed"}[body.gate]
                       if body.approved else "ready")
        if body.approved and body.gate == "release":
            guides.publish_for_release(hub, db, actor, task, request_id)
    run_id = uuid.uuid4()
    db.add(TaskRun(id=run_id, project_id=project_id, task_id=task_id, user_id=actor.user.id,
                   device_id=actor.device.id, instance_id=body.instance_id, harness=body.harness,
                   role=roles.GATE_ROLES[body.gate], lease_epoch=task.lease_epoch, state="finished", ended_at=now()))
    db.add(TaskDecision(project_id=project_id, task_id=task_id, author_id=actor.user.id, device_id=actor.device.id,
                        run_id=run_id, worklog_id=body.worklog_id, gate=body.gate, approved=body.approved,
                        evidence=body.evidence, override_reason=body.override_reason.strip(), task_version=task.version))
    task.version += 1
    db.flush()
    emit(hub, db, actor, task, "task.gate_decided", request_id, run_id=run_id, worklog_id=body.worklog_id)
    return task_view(task)
