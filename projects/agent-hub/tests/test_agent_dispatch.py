import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_repositories import grant, register as repository, task as repository_task
from test_tasks import claim, developer, log_body, ready

from agent_hub.db import context, session_factory
from agent_hub.models import AgentDispatch, AgentEndpoint, Grant, TaskRun
from agent_hub.security import now


def register(w, p, who=None, **extra):
    return w.ok("POST", f"/api/projects/{p}/agents", who=who or w.root,
                body={"instance_id": str(uuid.uuid4()), "harness": "codex", "name": "Web developer",
                      "roles": ["developer"], "expected_version": 0, **extra})


def dispatch(w, p, task, endpoint, **extra):
    return w.ok("POST", f"/api/projects/{p}/agent-dispatches", who=w.root, body={
        "task_id": task["id"], "endpoint_id": endpoint["id"], "expected_task_version": task["version"],
        "expected_endpoint_version": endpoint["version"], "note": "请核对任务范围与验收后决定是否接受", **extra})


def decision(w, p, row, action, who=None, **extra):
    return w.call("POST", f"/api/projects/{p}/agent-dispatches/{row['id']}/decision", who=who or w.root,
                  body={"action": action, "expected_version": row["version"], **extra})


def test_register_receive_accept_is_distinct_durable_and_idempotent(world):
    w, p = world, world.project()
    member = developer(w, p)
    endpoint = register(w, p, member)
    task = ready(w, p)
    row = dispatch(w, p, task, endpoint)
    assert row["state"] == "queued" and row["run_id"] is None and not row["automatic_execution"]
    assert w.ok("GET", f"/api/projects/{p}/agent-dispatches", who=member)["items"][0]["id"] == row["id"]
    received = decision(w, p, row, "receive", member).json()["dispatch"]
    assert received["state"] == "received" and received["run_id"] is None
    assert w.ok("GET", f"/api/projects/{p}/tasks/{task['id']}", who=w.root)["task"]["status"] == "ready"
    key = str(uuid.uuid4())
    body = {"action": "accept", "expected_version": received["version"]}
    path = f"/api/projects/{p}/agent-dispatches/{row['id']}/decision"
    result = w.ok("POST", path, who=member, body=body, idem=key)
    assert w.ok("POST", path, who=member, body=body, idem=key) == result
    assert result["dispatch"]["state"] == "accepted" and result["run"]["instance_id"] == endpoint["instance_id"]
    assert result["dispatch"]["run_id"] == result["run"]["id"]
    assert decision(w, p, result["dispatch"], "decline", member, reason="late").json()["code"] == "AGENT_DISPATCH_FINAL"


def test_registration_versions_frozen_identity_and_role_changes(world):
    w, p = world, world.project()
    endpoint = register(w, p)
    body = {k: endpoint[k] for k in ("instance_id", "harness", "name", "roles")}
    updated = w.ok("POST", f"/api/projects/{p}/agents", who=w.root,
                   body={**body, "expected_version": 1})
    assert updated["version"] == 1 and updated["recent_agent_activity"]
    row = dispatch(w, p, ready(w, p), endpoint)
    updated = w.ok("POST", f"/api/projects/{p}/agents", who=w.root,
                   body={**body, "expected_version": 1, "roles": ["designer"]})
    assert updated["version"] == 2
    assert decision(w, p, row, "accept").json()["code"] == "AGENT_DISPATCH_STALE"
    assert w.call("POST", f"/api/projects/{p}/agents", who=w.root,
                  body={**body, "expected_version": 2, "harness": "cursor"}).json()["code"] == "AGENT_IDENTITY_FROZEN"
    assert w.call("POST", f"/api/projects/{p}/agents/{endpoint['id']}/heartbeat", who=w.root,
                  body={"expected_version": 1}).json()["code"] == "AGENT_VERSION_CONFLICT"


def test_decline_cancel_and_expired_redelivery(world):
    w, p = world, world.project()
    member = developer(w, p)
    endpoint, task = register(w, p, member), ready(w, p)
    row = dispatch(w, p, task, endpoint)
    assert decision(w, p, row, "decline", member).status_code == 422
    assert decision(w, p, row, "decline", member, reason="当前正在其他工作").json()["dispatch"]["state"] == "declined"
    row = dispatch(w, p, task, endpoint)
    assert decision(w, p, row, "cancel", member, reason="test").status_code == 404
    assert decision(w, p, row, "cancel", reason="重新排期").json()["dispatch"]["state"] == "cancelled"
    row = dispatch(w, p, task, endpoint)
    with w.owner.begin() as db:
        # Administrative fixture simulates elapsed time without editing state.
        db.get(AgentDispatch, uuid.UUID(row["id"])).expires_at = now() - timedelta(seconds=1)
    assert decision(w, p, row, "accept", member).json()["code"] == "AGENT_DISPATCH_EXPIRED"
    assert dispatch(w, p, task, endpoint)["id"] != row["id"]


def test_duplicate_and_concurrent_claim_never_create_two_runs(world):
    w, p = world, world.project()
    member = developer(w, p)
    endpoint, task = register(w, p, member), ready(w, p)
    row = dispatch(w, p, task, endpoint)
    assert w.call("POST", f"/api/projects/{p}/agent-dispatches", who=w.root, body={
        "task_id": task["id"], "endpoint_id": endpoint["id"], "expected_task_version": task["version"],
        "expected_endpoint_version": 1, "note": "重复投递"}).json()["code"] == "AGENT_DISPATCH_ALREADY_PENDING"
    barrier = Barrier(2)
    def accept():
        barrier.wait(timeout=5)
        return decision(w, p, row, "accept", member)
    def direct():
        barrier.wait(timeout=5)
        return w.call("POST", f"/api/projects/{p}/tasks/{task['id']}/claim", who=w.root,
                      body={"instance_id": str(uuid.uuid4()), "harness": "human"})
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(accept), pool.submit(direct)]
        results = [f.result(timeout=20) for f in futures]
    assert sorted(r.status_code for r in results) == [200, 409]
    with w.owner() as db:
        assert db.scalar(select(text("count(*)")).select_from(TaskRun).where(TaskRun.task_id == task["id"], TaskRun.state == "active")) == 1


def test_accept_preserves_dependency_and_path_conflict_checks(world):
    w, p = world, world.project()
    endpoint = register(w, p)
    prerequisite = ready(w, p)
    task = ready(w, p, depends_on=[prerequisite["id"]])
    row = dispatch(w, p, task, endpoint)
    assert decision(w, p, row, "accept").json()["code"] == "TASK_DEPENDENCIES_INCOMPLETE"
    first = ready(w, p, execution_mode="observed")
    other = ready(w, p, execution_mode="observed")
    claim(w, p, first)
    row = dispatch(w, p, other, endpoint)
    assert decision(w, p, row, "accept").json()["code"] == "TASK_SCOPE_CONFLICT"


def test_revocation_and_expiry_remove_eligibility_and_cached_mutations(world):
    w, p = world, world.project()
    member = developer(w, p)
    endpoint = register(w, p, member)
    task = ready(w, p)
    row = dispatch(w, p, task, endpoint)
    path = f"/api/projects/{p}/agent-dispatches/{row['id']}/decision"
    key = str(uuid.uuid4())
    body = {"action": "receive", "expected_version": 1}
    w.ok("POST", path, who=member, body=body, idem=key)
    with w.owner.begin() as db:
        db.get(Grant, uuid.UUID(member.tokens["grant_id"])).expires_at = now() - timedelta(seconds=1)
    assert w.call("POST", path, who=member, body=body, idem=key).status_code in (401, 403, 404)
    assert w.ok("GET", f"/api/projects/{p}/agents", who=w.root)["items"] == []


def test_repository_scoping_hides_endpoints_dispatches_and_notes(world, database):
    w, p = world, world.project()
    web, pc = repository(w, p), repository(w, p, "pc")
    member = w.administrator(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write"])],
                          caps=["project:read", "tasks:claim", "tasks:write", "tasks:publish"]))
    hidden = register(w, p, repository_id=pc["id"], name="hidden-private-executor")
    hidden_task = repository_task(w, p, pc, title="hidden-private-title")
    dispatch(w, p, hidden_task, hidden, note="hidden-private-note")
    visible = register(w, p, member, repository_id=web["id"])
    for suffix in ("agents", "agent-dispatches"):
        data = w.ok("GET", f"/api/projects/{p}/{suffix}", who=member)
        assert "hidden-private" not in json.dumps(data) and hidden["id"] not in json.dumps(data)
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
            assert [str(a.id) for a in db.scalars(select(AgentEndpoint))] == [visible["id"]]
            assert not list(db.scalars(select(AgentDispatch)))
    finally:
        runtime.kw["bind"].dispose()
    assert w.call("POST", f"/api/projects/{p}/agents", who=member, body={
        "instance_id": str(uuid.uuid4()), "harness": "codex", "name": "test", "roles": ["developer"]}).status_code == 422


def test_other_device_cannot_receive_or_replay_registration(world):
    from conftest import Identity
    w, p = world, world.project()
    first = register(w, p)
    key, enrollment = w.enroll("root-admin")
    second = Identity(w.root.user_id, key, enrollment)
    w.email(second)
    w.start(second)
    row = dispatch(w, p, ready(w, p), first)
    assert decision(w, p, row, "receive", second).status_code == 404
    body = {"instance_id": str(uuid.uuid4()), "harness": "codex", "name": "device-bound", "roles": ["developer"], "expected_version": 0}
    idem = str(uuid.uuid4())
    one = w.ok("POST", f"/api/projects/{p}/agents", who=w.root, body=body, idem=idem)
    two = w.ok("POST", f"/api/projects/{p}/agents", who=second, body=body, idem=idem)
    assert one["id"] != two["id"] and one["device_id"] != two["device_id"]


def test_database_rejects_fabricated_acceptance_and_terminal_rewrite(world, database):
    w, p = world, world.project()
    endpoint = register(w, p)
    row = dispatch(w, p, ready(w, p), endpoint)
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            with pytest.raises(DBAPIError):
                db.execute(text("UPDATE agent_dispatches SET state='accepted',version=2,decided_at=now() WHERE id=:id"), {"id": row["id"]})
        done = decision(w, p, row, "cancel", reason="fixture cancelled").json()["dispatch"]
        with runtime() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            with pytest.raises(DBAPIError):
                db.execute(text("UPDATE agent_dispatches SET state='received',version=3 WHERE id=:id"), {"id": done["id"]})
    finally:
        runtime.kw["bind"].dispose()


def test_worklog_turn_reference_keeps_receipt_and_old_payload_compatible(world):
    w, p = world, world.project()
    claimed = claim(w, p, ready(w, p))
    path = f"/api/projects/{p}/tasks/{claimed['task']['id']}/worklogs"
    turn = str(uuid.uuid4())
    record = w.ok("POST", path, who=w.root, body=log_body(claimed, turn_id=turn))
    assert record["turn_id"] == turn
    old = w.ok("POST", path, who=w.root, body=log_body(claimed))
    assert "turn_id" not in old
