import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_closeouts import call, discover
from test_tasks import claim, developer, gate, new_task, ready
from test_work_policy import observed_body, prepare

from agent_hub import roles
from agent_hub.db import context, session_factory
from agent_hub.models import TaskRun


def test_catalog_is_authorized_versioned_and_localized(world):
    w, p = world, world.project()
    member = developer(w, p)
    path = f"/api/projects/{p}/roles"
    zh = w.ok("GET", path, who=member)
    en = w.ok("GET", path + "?language=en", who=member)
    assert zh["catalog_version"] == 1 and len(zh["roles"]) == 7
    assert zh["roles"][1]["name"] == "架构师" and en["roles"][1]["name"] == "Architect"
    assert zh["content_hash"] != en["content_hash"]
    assert all(r["grants_permissions"] is False for r in zh["roles"])
    assert w.call("GET", path + "?language=../en", who=member).status_code == 422
    assert w.call("GET", f"/api/projects/{uuid.uuid4()}/roles", who=member).status_code == 404
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    assert w.call("GET", path, who=member).status_code in (401, 403)


@pytest.mark.parametrize("role_id", roles.PRIMARY_ROLES)
def test_task_role_is_bound_to_claim_and_prepared_context(world, role_id):
    w, p = world, world.project()
    member = developer(w, p)
    task = ready(w, p, execution_role=role_id, execution_mode="observed")
    claimed = claim(w, p, task, member)
    assert claimed["task"]["execution_role"] == claimed["run"]["role"] == role_id
    prepared = prepare(w, p, claimed, member)
    current = prepared["payload"]["role"]
    assert prepared["payload"]["schema_version"] == 3
    assert current == roles.role(role_id)
    assert len(prepared["payload"]["role"]["responsibilities"]) > 0
    assert "roles" not in prepared["payload"]  # No full catalog in every task context.
    base = f"/api/projects/{p}/tasks/{task['id']}"
    assert w.call("POST", base + "/claim", who=w.root, body={
        "instance_id": str(uuid.uuid4()), "harness": "human"}).json()["code"] == "TASK_ALREADY_CLAIMED"
    assert w.call("POST", base + "/claim", who=member, body={
        "instance_id": str(uuid.uuid4()), "harness": "codex", "role": "publisher"}).status_code == 422
    w.ok("POST", base + "/worklogs", who=member, body=observed_body(claimed, prepared))


def test_default_role_and_gate_runs_preserve_actual_identity(world):
    w, p = world, world.project()
    task = new_task(w, p)
    assert task["execution_role"] == "developer"
    base = f"/api/projects/{p}/tasks/{task['id']}"
    for name in ("requirements", "design", "feasibility"):
        task = gate(w, p, task, name)
    result = w.ok("GET", base, who=w.root)
    runs = {r["id"]: r for r in result["runs"]}
    assert len(runs) == 3
    for decision in result["decisions"]:
        run = runs[decision["run_id"]]
        assert run["role"] == roles.GATE_ROLES[decision["gate"]]
        assert run["user_id"] == w.root.user_id
        assert run["device_id"] == w.root.enrollment["device_id"]
        assert run["state"] == "finished" and run["harness"] == "human"


def test_role_cannot_grant_task_or_repository_permissions(world):
    w, p = world, world.project()
    member = developer(w, p, caps=["project:read", "tasks:write"])
    task = ready(w, p, execution_role="project-manager")
    base = f"/api/projects/{p}/tasks/{task['id']}"
    assert w.call("POST", base + "/claim", who=member, body={
        "instance_id": str(uuid.uuid4()), "harness": "codex"}).status_code == 404
    assert w.call("POST", f"/api/projects/{p}/tasks", who=member, body={
        "title": "不能用职业获得发布权限", "execution_role": "project-manager",
        "description": "不能由职业标签提升权限", "acceptance": "拒绝创建",
        "scope": {"repository": "web", "platform": "web", "paths": ["src"]}}).status_code == 404
    for invalid in ("publisher", "reviewer", "tester", "admin"):
        assert w.call("POST", f"/api/projects/{p}/tasks", who=w.root, body={
            "title": "invalid role", "execution_role": invalid,
            "description": "invalid role", "acceptance": "reject",
            "scope": {"repository": "web", "platform": "web", "paths": ["src"]}}).status_code == 422


def test_changed_role_guidance_invalidates_context(world, monkeypatch):
    w, p = world, world.project()
    claimed = claim(w, p, ready(w, p, execution_role="architect", execution_mode="observed"))
    prepared = prepare(w, p, claimed)
    original = roles.role

    def updated(role_id, language="zh"):
        return {**original(role_id, language), "content_hash": "f" * 64}

    monkeypatch.setattr(roles, "role", updated)
    base = f"/api/projects/{p}/tasks/{claimed['task']['id']}"
    path = base + f"/contexts/{prepared['id']}/status?run_id={claimed['run']['id']}&lease_epoch={claimed['run']['lease_epoch']}"
    assert w.call("GET", path, who=w.root).json()["code"] == "WORK_ROLE_STALE"
    assert w.call("POST", base + "/worklogs", who=w.root, body=observed_body(claimed, prepared)).json()["code"] == "WORK_ROLE_STALE"


@pytest.mark.parametrize("role_id", ["project-manager", "architect", "designer"])
def test_revocation_freezes_every_primary_role_for_limited_handoff(world, role_id):
    w, p = world, world.project()
    member = developer(w, p)
    claimed = claim(w, p, ready(w, p, execution_role=role_id, execution_mode="observed"), member)
    prepared = prepare(w, p, claimed, member)
    task_id = claimed["task"]["id"]
    body = {**observed_body(claimed, prepared), "operation_id": str(uuid.uuid4()), "task_id": task_id}
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    jobs = discover(w, member)
    assert len(jobs) == 1
    response = call(w, member, jobs[0], "checkpoints", body)
    assert response.status_code == 200, response.text
    assert response.json()["record_kind"] == "revocation_handoff"
    result = w.ok("GET", f"/api/projects/{p}/tasks/{task_id}", who=w.root)
    assert result["task"]["status"] == "ready" and result["task"]["active_run_id"] is None
    assert next(r for r in result["runs"] if r["id"] == claimed["run"]["id"])["state"] == "released"


def test_rls_refuses_a_primary_run_with_a_forged_role(world, database):
    w, p = world, world.project()
    task = ready(w, p, execution_role="architect")
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            with pytest.raises(DBAPIError):
                db.execute(text("""INSERT INTO task_runs (id,project_id,task_id,user_id,device_id,instance_id,harness,role,lease_epoch,state)
                    VALUES (:id,:p,:t,:u,:d,:instance,'codex','developer',1,'active')"""),
                    {"id": str(uuid.uuid4()), "p": p, "t": task["id"], "u": w.root.user_id,
                     "d": w.root.enrollment["device_id"], "instance": str(uuid.uuid4())})
    finally:
        runtime.kw["bind"].dispose()
    with w.owner() as db:
        assert not list(db.scalars(select(TaskRun).where(TaskRun.state == "active")))
