import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_repositories import grant, register
from test_work_archives import compress, setup_archive, upload
from test_work_policy import observed_body, prepare

from agent_hub.db import context, session_factory
from agent_hub.models import CloseoutJob, Worklog
from agent_hub.security import now


def discover(w, who):
    return w.ok("POST", "/api/closeouts/discover", key=who.key, body={})["items"]


def call(w, who, job, suffix, body):
    return w.call("POST", f"/api/closeouts/{job['id']}/{suffix}", key=who.key,
        token=job["upload_token"], body=body)


def fixture(w):
    p, repo, task, run, member, log, archive, _ = setup_archive(w, member=True)
    prepared = prepare(w, p, run, member)
    body = observed_body(run, prepared)
    body.update(operation_id=str(uuid.uuid4()), task_id=task["id"],
        change_evidence=archive["evidence"], git_commit=archive["evidence"]["head_commit"])
    return p, repo, task, run, member, body, archive


def put(w, member, job, body, archive):
    response = call(w, member, job, "checkpoints", body)
    assert response.status_code == 200, response.text
    log = response.json()
    value = copy.deepcopy(archive)
    value["worklog_id"] = log["id"]
    who = copy.copy(member)
    who.tokens = {"access_token": job["upload_token"]}
    response = upload(w, f"/api/closeouts/{job['id']}/checkpoints/{log['id']}/snapshot", compress(value), who)
    assert response.status_code == 200, response.text
    return log, response.json()


@pytest.mark.parametrize("reason", ["member", "account", "device"])
def test_revoked_identity_can_only_handoff_then_authorized_peer_can_recover(world, reason):
    w = world
    p, repo, task, run, member, body, archive = fixture(w)
    if reason == "member":
        w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    elif reason == "account":
        w.ok("POST", f"/api/admin/users/{member.user_id}/status", who=w.root, body={"status": "disabled"})
    else:
        w.ok("POST", f"/api/admin/devices/{member.enrollment['device_id']}/revoke", who=w.root, body={})
    assert w.call("GET", f"/api/projects/{p}/tasks", who=member).status_code in (401, 403)
    job = discover(w, member)[0]
    assert not job["normal_operations_allowed"]
    released = w.ok("GET", f"/api/projects/{p}/tasks/{task['id']}", who=w.root)["task"]
    assert released["status"] == "ready" and released["active_run_id"] is None
    log, snapshot = put(w, member, job, body, archive)
    retry, snapshot2 = put(w, member, job, body, archive)
    assert retry["id"] == log["id"] and snapshot2["id"] == snapshot["id"]
    sealed = call(w, member, job, "seal", {"worklog_ids": [log["id"]]})
    assert sealed.status_code == 200, sealed.text
    seal = sealed.json()
    assert seal["server_code_backup"] and seal["checkpoints"] == 1
    path = f"/api/projects/{p}/tasks/{task['id']}/worklogs/{log['id']}/snapshot/content"
    assert w.call("GET", path, key=member.key, token=job["upload_token"]).json()["code"] == "INVALID_SESSION"
    peer = w.member(p, "handoff-peer")
    w.start(peer, grant(w, peer, p, [(repo, ["code:read"])]))
    assert w.call("GET", path, who=peer).content == compress({**archive, "worklog_id": log["id"]})
    result = call(w, member, job, "cleanup-report", {"seal_id": seal["id"], "receipt_hash": seal["receipt_hash"],
        "result": "complete", "removed_files": 2, "conflicts": 0})
    assert result.status_code == 200 and result.json()["cleanup_verification"] == "client_report_only"
    assert call(w, member, job, "checkpoints", {**body, "operation_id": str(uuid.uuid4())}).json()["code"] == "CLOSEOUT_ALREADY_COMPLETE"


def test_closeout_is_bound_to_device_frozen_run_and_immutable_payload(world):
    w = world
    p, repo, task, run, member, body, archive = fixture(w)
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    job = discover(w, member)[0]
    assert discover(w, w.root) == []
    assert call(w, w.root, job, "checkpoints", body).json()["code"] == "CLOSEOUT_NOT_ACCESSIBLE"
    for field in ("run_id", "task_id", "context_id"):
        response = call(w, member, job, "checkpoints", {**body, field: str(uuid.uuid4())})
        assert response.status_code in (403, 409)
    log, _ = put(w, member, job, body, archive)
    assert call(w, member, job, "checkpoints", {**body, "notes": "different"}).json()["code"] == "IDEMPOTENCY_PAYLOAD_CHANGED"
    assert call(w, member, job, "seal", {"worklog_ids": []}).json()["code"] == "CLOSEOUT_INVENTORY_MISMATCH"
    assert call(w, member, job, "cleanup-report", {"seal_id": str(uuid.uuid4()), "receipt_hash": "a" * 64,
        "result": "complete", "removed_files": 0, "conflicts": 0}).json()["code"] == "CLOSEOUT_UPLOAD_CONFIRMATION_REQUIRED"
    assert log["record_kind"] == "revocation_handoff"


def test_partial_upload_or_expiry_cannot_authorize_cleanup(world):
    w = world
    p, _, _, _, member, body, _ = fixture(w)
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    job = discover(w, member)[0]
    log = call(w, member, job, "checkpoints", body).json()
    assert call(w, member, job, "seal", {"worklog_ids": [log["id"]]}).json()["code"] == "CLOSEOUT_CODE_BACKUP_REQUIRED"
    with w.owner.begin() as db:
        db.get(CloseoutJob, uuid.UUID(job["id"])).expires_at = now() - timedelta(seconds=1)
    assert call(w, member, job, "checkpoints", body).json()["code"] == "CLOSEOUT_EXPIRED"
    assert w.call("POST", f"/api/admin/closeouts/{job['id']}/extend", who=member, body={"days": 7}).status_code != 200
    w.ok("POST", f"/api/admin/closeouts/{job['id']}/extend", who=w.root, body={"days": 7})
    assert call(w, member, job, "checkpoints", body).status_code == 200


def test_grant_replacement_only_closes_removed_repository_and_expiry_creates_no_job(world):
    w = world
    p, repo, _, _, member, _, _ = fixture(w)
    pc = register(w, p, "pc")
    grant(w, member, p, [(repo, ["code:read", "code:write"]), (pc, ["code:read", "code:write"])])
    assert discover(w, member) == []
    grant(w, member, p, [(pc, ["code:read", "code:write"])])
    jobs = discover(w, member)
    assert len(jobs) == 1 and jobs[0]["scope"]["repository_ids"] == [repo["id"]]


def test_closeout_context_does_not_open_normal_rows_or_other_authors(world, database):
    from agent_hub.closeouts import authenticate
    from agent_hub.security import public_key
    w = world
    p, _, task, _, member, body, _ = fixture(w)
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    job = discover(w, member)[0]
    call(w, member, job, "checkpoints", body)
    runtime = session_factory(database[0])
    with runtime.begin() as db:
        authenticate(w.client.app.state.hub, db, public_key(member.key.jwk)[1], job["upload_token"], uuid.UUID(job["id"]))
        rows = list(db.scalars(select(Worklog)))
        assert len(rows) == 1 and rows[0].closeout_id == uuid.UUID(job["id"])
        from agent_hub.models import Task
        assert list(db.scalars(select(Task))) == []
        context(db, member.user_id, member.enrollment["device_id"])
    runtime.kw["bind"].dispose()


def test_prior_acknowledged_context_remains_uploadable_and_new_log_invalidates_old_seal(world):
    from agent_hub.models import WorkContext
    w = world
    p, _, _, run, member, body, archive = fixture(w)
    with w.owner() as db:
        prior = db.scalar(select(WorkContext).where(WorkContext.run_id == uuid.UUID(run["run"]["id"]))
            .order_by(WorkContext.created_at)).id
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    job = discover(w, member)[0]
    log, _ = put(w, member, job, {**body, "context_id": str(prior)}, archive)
    sealed = call(w, member, job, "seal", {"worklog_ids": [log["id"]]}).json()
    extra = call(w, member, job, "checkpoints", {**body, "operation_id": str(uuid.uuid4())})
    assert extra.status_code == 200
    report = call(w, member, job, "cleanup-report", {"seal_id": sealed["id"], "receipt_hash": sealed["receipt_hash"],
        "result": "complete", "removed_files": 1, "conflicts": 0})
    assert report.json()["code"] == "CLOSEOUT_UPLOAD_CONFIRMATION_REQUIRED"


def test_write_permission_reduction_releases_task_and_archiving_also_covers_superadmin_runs(world):
    w = world
    p, repo, task, _, member, _, _ = fixture(w)
    grant(w, member, p, [(repo, ["code:read"])])
    assert discover(w, member)[0]["scope"]["repository_ids"] == [repo["id"]]
    from test_tasks import claim
    claim(w, p, task, w.root)
    w.ok("POST", f"/api/projects/{p}/repositories/{repo['id']}/status", who=w.root,
        body={"status": "archived", "expected_version": repo["version"]})
    jobs = discover(w, w.root)
    assert jobs and jobs[0]["scope"]["repository_versions"][repo["id"]] == repo["version"]
