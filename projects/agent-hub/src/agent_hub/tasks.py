"""Project-serialized task aggregates. Narrative progress never changes task state."""

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from . import repositories, work_policy
from .models import ProjectProgress, Task, TaskDecision, TaskRun, Worklog
from .security import HubError, now

LEASE_SECONDS = 600
ADMISSION = ("requirements", "design", "feasibility")


def task_view(t):
    return {"id": str(t.id), "project_id": str(t.project_id), "parent_id": str(t.parent_id) if t.parent_id else None,
            "repository_id": str(t.repository_id) if t.repository_id else None,
            "title": t.title, "description": t.description, "acceptance": t.acceptance, "scope": t.scope,
            "execution_mode": t.execution_mode,
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
    return value


def log_view(w):
    return {**log_payload(w), "receipt_hash": w.receipt_hash, "code_backup": False,
            "git_commit_verified": False}


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


def page(db, project_id, offset, limit, *, repository_id=None):
    statement = select(Task).where(Task.project_id == project_id)
    if repository_id:
        statement = statement.where(Task.repository_id == repository_id)
    rows = list(db.scalars(statement
                          .order_by(Task.created_at.desc(), Task.id).offset(offset).limit(limit)))
    return {"items": [task_view(t) for t in rows], "has_more": len(rows) == limit, "next_offset": offset + len(rows)}


def detail(db, project_id, task_id):
    task = get_task(db, project_id, task_id)
    runs = db.scalars(select(TaskRun).where(TaskRun.task_id == task_id, TaskRun.project_id == project_id)
                      .order_by(TaskRun.created_at.desc(), TaskRun.id).limit(100))
    decisions = db.scalars(select(TaskDecision).where(TaskDecision.task_id == task_id, TaskDecision.project_id == project_id)
                          .order_by(TaskDecision.created_at.desc(), TaskDecision.id).limit(100))
    return {"task": task_view(task), "runs": [run_view(r) for r in runs],
            "decisions": [decision_view(d) for d in decisions]}


def emit(hub, db, actor, task, kind, request_id, *, run_id=None, worklog_id=None):
    # Full notes and gate evidence stay behind logs:read; the shared stream has only state.
    label = {"task.created": "任务已创建", "task.claimed": "任务已认领", "task.released": "认领已释放",
             "task.submitted": "任务待评审", "worklog.recorded": "工作日志已保存", "task.gate_decided": "流程已推进"}[kind]
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
    task = Task(project_id=project_id, created_by=actor.user.id, **body.model_dump(mode="python"))
    db.add(task)
    db.flush()
    emit(hub, db, actor, task, "task.created", request_id)
    return task_view(task)


def claim(hub, db, actor, project_id, task_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:claim")
    hub.project(db, actor, project_id, "tasks:write")
    task = get_task(db, project_id, task_id)
    if task.repository_id:
        repositories.require(db, project_id, task.repository_id, "code:write")
    if task.status not in {"ready", "in_progress"}:
        raise HubError("TASK_NOT_CLAIMABLE", 409, task_status=task.status)
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
                  role="developer", lease_epoch=task.lease_epoch)
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
    return {"items": [log_view(w) for w in rows], "has_more": len(rows) == limit, "next_offset": offset + len(rows)}


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
    return log_view(row)


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
    task.submission_id, task.status, task.version = latest.id, "review", task.version + 1
    task.active_run_id, task.lease_until = None, None
    run.state, run.ended_at = "submitted", now()
    emit(hub, db, actor, task, "task.submitted", request_id, run_id=run.id, worklog_id=latest.id)
    return task_view(task)


def decide(hub, db, actor, project_id, task_id, body, request_id):
    cap = {"review": "tasks:review", "test": "tasks:test"}.get(body.gate, "tasks:publish")
    hub.project(db, actor, project_id, cap)
    task = get_task(db, project_id, task_id)
    check_version(task, body.expected_version)
    run_id = None
    if body.gate in ADMISSION:
        if task.status != "draft" or task.admission_step >= 3 or body.gate != ADMISSION[task.admission_step]:
            raise HubError("TASK_GATE_ORDER_INVALID", 409)
        if body.worklog_id or body.override_reason:
            raise HubError("TASK_GATE_INPUT_INVALID", 422)
        if body.approved:
            task.admission_step += 1
            if task.admission_step == 3:
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
        run_id = uuid.uuid4()
        db.add(TaskRun(id=run_id, project_id=project_id, task_id=task_id, user_id=actor.user.id,
                       device_id=actor.device.id, instance_id=body.instance_id, harness=body.harness,
                       role={"review": "reviewer", "test": "tester", "release": "publisher"}[body.gate],
                       lease_epoch=task.lease_epoch, state="finished", ended_at=now()))
        if body.approved and body.gate == "release":
            pending = db.scalar(select(Task.id).where(Task.parent_id == task_id, Task.project_id == project_id,
                                                     Task.status != "completed").limit(1))
            if pending:
                raise HubError("TASK_CHILDREN_INCOMPLETE", 409)
        task.status = ({"review": "testing", "test": "release_ready", "release": "completed"}[body.gate]
                       if body.approved else "ready")
    db.add(TaskDecision(project_id=project_id, task_id=task_id, author_id=actor.user.id, device_id=actor.device.id,
                        run_id=run_id, worklog_id=body.worklog_id, gate=body.gate, approved=body.approved,
                        evidence=body.evidence, override_reason=body.override_reason.strip(), task_version=task.version))
    task.version += 1
    db.flush()
    emit(hub, db, actor, task, "task.gate_decided", request_id, run_id=run_id, worklog_id=body.worklog_id)
    return task_view(task)
