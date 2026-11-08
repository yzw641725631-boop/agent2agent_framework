import hashlib
import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from agent_hub.db import context, session_factory
from agent_hub.models import ProjectPolicy, WorkContext
from test_tasks import claim, developer, gate, lease, log_body, ready


def prepare(w, project, claimed, who=None, acknowledge=True):
    actor = who or w.root
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    result = w.ok("POST", base + "/contexts", who=actor, body=lease(claimed))
    if acknowledge:
        w.ok("POST", base + "/context-acknowledgments", who=actor, body={**lease(claimed),
             "context_id": result["id"], "content_hash": result["content_hash"],
             "rule_ids": [r["id"] for r in result["payload"]["policy"]["rules"]]})
    return result


def observed_body(claimed, prepared, **overrides):
    return log_body(claimed, context_id=prepared["id"], git_commit="b" * 40,
        change_evidence={"schema_version": 1, "source": "desktop-git-observer", "base_commit": "a" * 40,
                         "head_commit": "b" * 40, "files": [{"path": "src/login.ts", "status": "modified",
                         "sha256": "c" * 64, "bytes": 20, "mode": "100644"}]}, **overrides)


def publish(w, project, version=0, **values):
    return w.ok("PUT", f"/api/projects/{project}/policy", who=w.root,
                body={"expected_version": version, "protected_paths": ["src/frozen"],
                      "forbidden_features": ["PC 客户端"], "rules": [{"id": "P-WEB", "text": "仅开发 Web。"}],
                      "change_reason": "按业务要求保护已有模块与目标端。", **values})


def test_new_code_tasks_require_prepared_acknowledged_context(world):
    w = world
    project = w.project()
    task = ready(w, project, execution_mode="observed")
    claimed = claim(w, project, task)
    base = f"/api/projects/{project}/tasks/{task['id']}"
    assert w.call("POST", base + "/worklogs", who=w.root, body=log_body(claimed)).json()["code"] == "WORK_CONTEXT_REQUIRED"
    prepared = prepare(w, project, claimed, acknowledge=False)
    response = w.call("POST", base + "/worklogs", who=w.root, body=observed_body(claimed, prepared))
    assert response.json()["code"] == "WORK_CONTEXT_ACK_REQUIRED"
    assert prepared["payload"]["task"]["scope"]["platform"] == "web"
    assert prepared["payload"]["memory_mode"].endswith("no automatic distillation")


def test_ack_must_name_all_current_rules_and_correct_content(world):
    w = world
    project = w.project()
    claimed = claim(w, project, ready(w, project, execution_mode="observed"))
    prepared = prepare(w, project, claimed, acknowledge=False)
    path = f"/api/projects/{project}/tasks/{claimed['task']['id']}/context-acknowledgments"
    body = {**lease(claimed), "context_id": prepared["id"], "content_hash": prepared["content_hash"], "rule_ids": []}
    assert w.call("POST", path, who=w.root, body=body).json()["code"] == "WORK_CONTEXT_ACK_MISMATCH"
    body["rule_ids"] = [r["id"] for r in prepared["payload"]["policy"]["rules"]]
    assert w.call("POST", path, who=w.root, body={**body, "content_hash": "0" * 64}).json()["code"] == "WORK_CONTEXT_ACK_MISMATCH"
    assert w.ok("POST", path, who=w.root, body=body)["can_record"]


def test_policy_revision_invalidates_prepared_context_and_is_append_only(world, database):
    w = world
    project = w.project()
    claimed = claim(w, project, ready(w, project, execution_mode="observed"))
    prepared = prepare(w, project, claimed)
    policy = publish(w, project)
    assert policy["version"] == 1 and any(r["id"] == "B-SCOPE" for r in policy["rules"])
    path = f"/api/projects/{project}/tasks/{claimed['task']['id']}/worklogs"
    assert w.call("POST", path, who=w.root, body=observed_body(claimed, prepared)).json()["code"] == "WORK_CONTEXT_STALE"
    newer = prepare(w, project, claimed)
    assert newer["payload"]["policy"]["version"] == 1
    w.ok("POST", path, who=w.root, body=observed_body(claimed, newer))
    response = w.call("PUT", f"/api/projects/{project}/policy", who=w.root, body={
        "expected_version": 0, "change_reason": "基于旧版本的修改应当被拒绝。"})
    assert response.json()["code"] == "POLICY_VERSION_CONFLICT"
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE project_policies SET content='{}'::jsonb"))
    runtime.kw["bind"].dispose()


def test_policy_publish_requires_project_publish_capability(world):
    w = world
    project = w.project()
    member = developer(w, project)
    path = f"/api/projects/{project}/policy"
    assert w.ok("GET", path, who=member)["version"] == 0
    assert w.call("PUT", path, who=member, body={"expected_version": 0, "change_reason": "成员不能直接更改项目保护规则。"}).status_code == 404


def test_observed_manifest_must_match_paths_and_protected_paths(world):
    w = world
    project = w.project()
    publish(w, project)
    claimed = claim(w, project, ready(w, project, execution_mode="observed"))
    prepared = prepare(w, project, claimed)
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    body = observed_body(claimed, prepared)
    assert w.call("POST", base + "/worklogs", who=w.root, body={**body, "changed_paths": []}).json()["code"] == "WORK_CHANGE_PATHS_MISMATCH"
    for file in ["src/frozen/main.ts", "SRC/FROZEN/main.ts", "src/.env", "src/secrets.pem"]:
        bad = json.loads(json.dumps(body))
        bad["changed_paths"] = [file]
        bad["change_evidence"]["files"][0]["path"] = file
        assert w.call("POST", base + "/worklogs", who=w.root, body=bad).json()["code"] == "WORK_PATH_PROTECTED"
    assert w.call("POST", base + "/worklogs", who=w.root, body={**body, "git_commit": "d" * 40}).json()["code"] == "WORK_GIT_REFERENCE_MISMATCH"


def test_observed_receipt_and_submit_bind_evidence_hash(world):
    w = world
    project = w.project()
    claimed = claim(w, project, ready(w, project, execution_mode="observed"))
    prepared = prepare(w, project, claimed)
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    note = w.ok("POST", base + "/worklogs", who=w.root, body=observed_body(claimed, prepared))
    expected = hashlib.sha256(json.dumps(note["change_evidence"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert note["evidence_hash"] == expected and note["code_backup"] is False and note["git_commit_verified"] is False
    submission = {**lease(claimed), "worklog_id": note["id"], "expected_version": claimed["task"]["version"]}
    assert w.call("POST", base + "/submit", who=w.root, body=submission).json()["code"] == "WORK_EVIDENCE_CHANGED"
    assert w.ok("POST", base + "/submit", who=w.root, body={**submission, "evidence_hash": expected})["status"] == "review"


def test_policy_change_after_log_prevents_submit_and_requires_new_log(world):
    w = world
    project = w.project()
    claimed = claim(w, project, ready(w, project, execution_mode="observed"))
    prepared = prepare(w, project, claimed)
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    note = w.ok("POST", base + "/worklogs", who=w.root, body=observed_body(claimed, prepared))
    publish(w, project)
    body = {**lease(claimed), "worklog_id": note["id"], "expected_version": claimed["task"]["version"], "evidence_hash": note["evidence_hash"]}
    assert w.call("POST", base + "/submit", who=w.root, body=body).json()["code"] == "WORK_CONTEXT_STALE"


def test_context_is_bound_to_run_and_hidden_from_other_users(world, database):
    w = world
    project = w.project()
    member, other = developer(w, project), developer(w, project, "other-member")
    claimed = claim(w, project, ready(w, project, execution_mode="observed"), member)
    prepared = prepare(w, project, claimed, member)
    path = f"/api/projects/{project}/tasks/{claimed['task']['id']}/contexts"
    assert w.call("POST", path, who=other, body=lease(claimed)).json()["code"] == "TASK_LEASE_NOT_OWNED"
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, other.user_id, other.enrollment["device_id"], other.tokens["grant_id"])
        assert list(db.scalars(select(WorkContext))) == []
        db.rollback()
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert db.get(WorkContext, uuid.UUID(prepared["id"]))
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE work_contexts SET payload='{}'::jsonb"))
    runtime.kw["bind"].dispose()


def test_log_index_is_bounded_and_full_read_respects_original_log_permissions(world):
    w = world
    project = w.project()
    member, reader = developer(w, project), developer(w, project, "index-only", caps=["project:read"])
    claimed = claim(w, project, ready(w, project, execution_mode="observed"), member)
    prepared = prepare(w, project, claimed, member)
    base = f"/api/projects/{project}/tasks/{claimed['task']['id']}"
    log = w.ok("POST", base + "/worklogs", who=member, body=observed_body(claimed, prepared))
    indexed = w.ok("GET", base + "/worklog-index", who=member)
    assert indexed["items"][0]["id"] == log["id"] and "notes" not in indexed["items"][0]
    assert w.ok("GET", base + "/worklog-index", who=reader)["items"] == []
    assert w.call("GET", base + "/worklogs/" + log["id"], who=reader).status_code == 404
    assert w.ok("GET", base + "/worklogs/" + log["id"], who=member)["notes"] == "private-worklog-body"
