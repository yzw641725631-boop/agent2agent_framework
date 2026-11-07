import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from agent_hub.db import context, session_factory
from agent_hub.models import Task, TaskDecision, TaskRun, Worklog
from agent_hub.security import now


def new_task(w, project, **overrides):
    return w.ok("POST", f"/api/projects/{project}/tasks", who=w.root, body={
        "title": "Web 登录页", "description": "仅实现 Web 登录", "acceptance": "登录校验与可访问性通过",
        "scope": {"repository": "web", "platform": "web", "paths": ["src", "tests"]}, **overrides})


def gate(w, project, task, name, *, who=None, **overrides):
    return w.ok("POST", f"/api/projects/{project}/tasks/{task['id']}/decisions", who=who or w.root, body={
        "gate": name, "approved": True, "expected_version": task["version"],
        "worklog_id": task["submission_id"], "evidence": "已核对需求、变更和验证证据。",
        "instance_id": str(uuid.uuid4()), "harness": "human", **overrides})


def ready(w, project, **overrides):
    task = new_task(w, project, **overrides)
    for name in ["requirements", "design", "feasibility"]:
        task = gate(w, project, task, name)
    return task


def developer(w, project, login="member-one", caps=None):
    member = w.member(project, login=login)
    return w.start(member, w.grant(member, project, caps or ["project:read", "tasks:claim", "tasks:write"]))


def claim(w, project, task, who=None):
    return w.ok("POST", f"/api/projects/{project}/tasks/{task['id']}/claim", who=who or w.root,
                body={"instance_id": str(uuid.uuid4()), "harness": "codex"})


def lease(claimed):
    return {"run_id": claimed["run"]["id"], "lease_epoch": claimed["run"]["lease_epoch"]}


def log_body(claimed, **overrides):
    return {**lease(claimed), "summary": "完成登录校验", "notes": "private-worklog-body",
            "changed_paths": ["src/login.ts"], "tests": "测试通过 3 项", "next_steps": "请复核边界条件",
            **overrides}


def submit(w, project, claimed, who=None):
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    log = w.ok("POST", base + "/worklogs", who=who or w.root, body=log_body(claimed))
    task = w.ok("POST", base + "/submit", who=who or w.root, body={**lease(claimed),
                 "worklog_id": log["id"], "expected_version": claimed["task"]["version"]})
    return task, log


def test_admission_order_and_progress_cannot_complete_task(world):
    w = world
    project = w.project()
    task = new_task(w, project)
    path = f"/api/projects/{project}/tasks/{task['id']}"
    assert w.call("POST", path + "/claim", who=w.root,
                  body={"instance_id": str(uuid.uuid4()), "harness": "human"}).json()["code"] == "TASK_NOT_CLAIMABLE"
    bad_gate = {"gate": "design", "approved": True, "expected_version": 1, "evidence": "premature",
                "instance_id": str(uuid.uuid4()), "harness": "human"}
    assert w.call("POST", path + "/decisions", who=w.root, body=bad_gate).json()["code"] == "TASK_GATE_ORDER_INVALID"
    w.ok("POST", f"/api/projects/{project}/progress", who=w.root,
         body={"summary": "completed", "status": "completed", "task_reference": task["id"]})
    assert w.ok("GET", path, who=w.root)["task"]["status"] == "draft"
    for name in ["requirements", "design", "feasibility"]:
        task = gate(w, project, task, name)
    assert task["status"] == "ready" and task["admission_step"] == 3


def test_concurrent_human_and_agent_claim_exactly_one_wins(world):
    w = world
    project = w.project()
    task = ready(w, project)
    member = developer(w, project)
    barrier = Barrier(2)

    def compete(identity, harness):
        barrier.wait(timeout=5)
        return w.call("POST", f"/api/projects/{project}/tasks/{task['id']}/claim", who=identity,
                      body={"instance_id": str(uuid.uuid4()), "harness": harness})

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(compete, w.root, "human"), pool.submit(compete, member, "codex")]
        responses = [f.result(timeout=15) for f in futures]
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()["code"] == "TASK_ALREADY_CLAIMED"
    with w.client.app.state.sessions() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        assert len(list(db.scalars(select(TaskRun).where(TaskRun.state == "active")))) == 1


def test_claim_retry_is_idempotent_but_new_instance_conflicts(world):
    w = world
    project = w.project()
    task = ready(w, project)
    path = f"/api/projects/{project}/tasks/{task['id']}/claim"
    body = {"instance_id": str(uuid.uuid4()), "harness": "codex"}
    key = str(uuid.uuid4())
    first = w.ok("POST", path, who=w.root, body=body, idem=key)
    assert w.ok("POST", path, who=w.root, body=body, idem=key) == first
    assert w.call("POST", path, who=w.root, body=body).json()["code"] == "TASK_ALREADY_CLAIMED"
    assert w.call("POST", path, who=w.root, body={**body, "harness": "human"}, idem=key).json()["code"] == "IDEMPOTENCY_PAYLOAD_CHANGED"


def test_expired_takeover_fences_old_heartbeat_logs_and_submit(world):
    w = world
    project = w.project()
    task = ready(w, project)
    old = claim(w, project, task)
    member = developer(w, project)
    base = f"/api/projects/{project}/tasks/{task['id']}"
    with w.client.app.state.sessions.begin() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        db.get(Task, uuid.UUID(task["id"])).lease_until = now() - timedelta(seconds=1)
    assert w.call("POST", base + "/heartbeat", who=w.root, body=lease(old)).json()["code"] == "TASK_LEASE_EXPIRED"
    newer = claim(w, project, task, member)
    assert newer["run"]["lease_epoch"] > old["run"]["lease_epoch"]
    for suffix, body in [("heartbeat", lease(old)), ("worklogs", log_body(old)),
                         ("submit", {**lease(old), "expected_version": old["task"]["version"], "worklog_id": str(uuid.uuid4())})]:
        assert w.call("POST", base + "/" + suffix, who=w.root, body=body).json()["code"] == "TASK_LEASE_STALE"
    assert w.ok("POST", base + "/heartbeat", who=member, body=lease(newer))["task"]["active_run_id"] == newer["run"]["id"]


def test_worklog_receipt_privacy_scope_and_database_immutability(world, database):
    w = world
    project = w.project()
    task = ready(w, project)
    dev = developer(w, project)
    outsider = developer(w, project, "reader-only", caps=["project:read"])
    claimed = claim(w, project, task, dev)
    path = f"/api/projects/{project}/tasks/{task['id']}/worklogs"
    assert w.call("POST", path, who=outsider, body=log_body(claimed)).status_code == 404
    assert w.call("POST", path, who=dev, body=log_body(claimed, changed_paths=["pc/main.ts"])).json()["code"] == "TASK_PATH_OUT_OF_SCOPE"
    key = str(uuid.uuid4())
    note = w.ok("POST", path, who=dev, body=log_body(claimed), idem=key)
    assert w.ok("POST", path, who=dev, body=log_body(claimed), idem=key) == note
    payload = {k: v for k, v in note.items() if k not in {"receipt_hash", "code_backup", "git_commit_verified"}}
    assert hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == note["receipt_hash"]
    assert note["code_backup"] is False
    assert w.ok("GET", path, who=outsider)["items"] == []
    assert w.ok("GET", path, who=dev)["visibility"] == "own"
    assert w.ok("GET", path, who=w.root)["items"][0] == note
    public = w.ok("GET", f"/api/projects/{project}/progress", who=outsider)
    assert "private-worklog-body" not in json.dumps(public)
    runtime = session_factory(database[0])
    with runtime() as db:
        assert list(db.scalars(select(Worklog))) == []
        context(db, dev.user_id, dev.enrollment["device_id"], dev.tokens["grant_id"])
        assert len(list(db.scalars(select(Worklog)))) == 1
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE worklogs SET notes='tampered'"))
        db.rollback()
    runtime.kw["bind"].dispose()


def test_cross_project_parent_and_foreign_task_access_denied(world, database):
    w = world
    project, other = w.project(), w.project("other-project")
    task = ready(w, other)
    member = developer(w, project)
    path = f"/api/projects/{project}/tasks/{task['id']}"
    assert w.call("GET", path, who=member).status_code == 404
    assert w.call("POST", path + "/claim", who=member,
                  body={"instance_id": str(uuid.uuid4()), "harness": "codex"}).status_code == 404
    assert w.call("POST", f"/api/projects/{project}/tasks", who=w.root, body={
        "title": "child", "description": "child", "acceptance": "test", "parent_id": task["id"],
        "scope": {"repository": "web", "platform": "web", "paths": ["src"]}}).status_code == 404
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert list(db.scalars(select(Task))) == []
    runtime.kw["bind"].dispose()


def test_revoked_member_cannot_heartbeat_or_write_closeout_not_faked(world):
    w = world
    project = w.project()
    task = ready(w, project)
    member = developer(w, project)
    claimed = claim(w, project, task, member)
    w.ok("POST", f"/api/projects/{project}/members/{member.user_id}/revoke", who=w.root, body={})
    base = f"/api/projects/{project}/tasks/{task['id']}"
    for suffix, body in [("heartbeat", lease(claimed)), ("worklogs", log_body(claimed))]:
        assert w.call("POST", base + "/" + suffix, who=member, body=body).json()["code"] == "PROJECT_ACCESS_REVOKED"


def test_gates_bind_latest_checkpoint_and_independent_author(world):
    w = world
    project = w.project()
    task = ready(w, project)
    dev = developer(w, project, caps=["project:read", "tasks:claim", "tasks:write", "tasks:review", "logs:read"])
    claimed = claim(w, project, task, dev)
    task, log = submit(w, project, claimed, dev)
    base = f"/api/projects/{project}/tasks/{task['id']}"
    body = {"gate": "review", "approved": True, "expected_version": task["version"], "worklog_id": log["id"],
            "evidence": "reviewed", "instance_id": str(uuid.uuid4()), "harness": "claude-code"}
    assert w.call("POST", base + "/decisions", who=dev, body=body).json()["code"] == "TASK_INDEPENDENT_REVIEW_REQUIRED"
    assert w.call("POST", base + "/decisions", who=w.root, body={**body, "worklog_id": str(uuid.uuid4())}).json()["code"] == "TASK_GATE_ORDER_INVALID"
    task = gate(w, project, task, "review")
    assert task["status"] == "testing"
    assert w.call("POST", base + "/decisions", who=w.root, body=body).json()["code"] == "TASK_VERSION_CONFLICT"
    task = gate(w, project, task, "test")
    assert task["status"] == "release_ready"
    task = gate(w, project, task, "release")
    assert task["status"] == "completed"
    assert w.ok("GET", base + "/worklogs", who=w.root)["items"][0]["id"] == log["id"]


def test_root_self_review_requires_explicit_audited_override(world):
    w = world
    project = w.project()
    task = ready(w, project)
    task, _ = submit(w, project, claim(w, project, task))
    task = gate(w, project, task, "review", override_reason="单人项目，由超管承担本次自审责任。")
    detail = w.ok("GET", f"/api/projects/{project}/tasks/{task['id']}", who=w.root)
    assert detail["decisions"][0]["override_reason"]
    assert detail["runs"][0]["role"] == "reviewer"


def test_failed_test_requires_new_run_and_review_again(world):
    w = world
    project = w.project()
    dev = developer(w, project)
    task = ready(w, project)
    task, old_log = submit(w, project, claim(w, project, task, dev), dev)
    task = gate(w, project, task, "review")
    task = gate(w, project, task, "test", approved=False, evidence="边界校验失败，需要修复")
    assert task["status"] == "ready"
    claimed = claim(w, project, task, dev)
    assert claimed["task"]["submission_id"] is None
    path = f"/api/projects/{project}/tasks/{task['id']}/submit"
    assert w.call("POST", path, who=dev, body={**lease(claimed), "worklog_id": old_log["id"],
                  "expected_version": claimed["task"]["version"]}).json()["code"] == "TASK_LATEST_CHECKPOINT_REQUIRED"
    task, _ = submit(w, project, claimed, dev)
    assert task["status"] == "review"


def test_parent_cannot_complete_with_pending_child(world):
    w = world
    project = w.project()
    dev = developer(w, project)
    task = ready(w, project)
    new_task(w, project, parent_id=task["id"])
    task, _ = submit(w, project, claim(w, project, task, dev), dev)
    for name in ["review", "test"]:
        task = gate(w, project, task, name)
    response = w.call("POST", f"/api/projects/{project}/tasks/{task['id']}/decisions", who=w.root,
                      body={"gate": "release", "approved": True, "expected_version": task["version"],
                            "worklog_id": task["submission_id"], "evidence": "release",
                            "instance_id": str(uuid.uuid4()), "harness": "human"})
    assert response.json()["code"] == "TASK_CHILDREN_INCOMPLETE"


def test_latest_round_scope_size_and_other_user_lease_are_enforced(world):
    w = world
    project = w.project()
    task = ready(w, project)
    member = developer(w, project)
    claimed = claim(w, project, task)
    base = f"/api/projects/{project}/tasks/{task['id']}"
    assert w.call("POST", base + "/heartbeat", who=member, body=lease(claimed)).json()["code"] == "TASK_LEASE_NOT_OWNED"
    oversized = log_body(claimed, notes="记" * 16000, tests="验" * 8000)
    assert w.call("POST", base + "/worklogs", who=w.root, body=oversized).status_code == 422
    first = w.ok("POST", base + "/worklogs", who=w.root, body=log_body(claimed))
    w.ok("POST", base + "/worklogs", who=w.root, body=log_body(claimed, outcome="blocked"))
    assert w.call("POST", base + "/submit", who=w.root, body={**lease(claimed),
                  "expected_version": claimed["task"]["version"], "worklog_id": first["id"]}).json()["code"] == "TASK_LATEST_CHECKPOINT_REQUIRED"


def test_activity_pagination_is_latest_first_and_permission_scoped(world):
    w = world
    project, other = w.project(), w.project("other-project")
    ready(w, project)
    member = developer(w, project, caps=["project:read"])
    path = f"/api/projects/{project}/activity"
    first = w.ok("GET", path + "?limit=2", who=member)
    older = w.ok("GET", path + "?limit=2&before=" + first["before"], who=member)
    assert len(first["items"]) == len(older["items"]) == 2
    assert min(int(e["sequence"]) for e in first["items"]) > max(int(e["sequence"]) for e in older["items"])
    assert w.call("GET", f"/api/projects/{other}/activity", who=member).status_code == 404
