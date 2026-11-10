"""Audit authorization, immutable reports and gates use the isolated real PostgreSQL service."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_delivery_evidence import checkpoint, configure, verify
from test_repositories import grant, register
from test_tasks import gate

from agent_hub.code_audit import AuditReport, CodeAudit
from agent_hub.db import context
from agent_hub.security import now


def policy_body(**overrides):
    return {
        "expected_version": 0,
        "enabled": True,
        "rules": [
            {"id": "layers", "title": "接口与分层", "requirement": "界面不得直接读取数据库或提升项目权限。"}
        ],
        "blocking_severities": ["critical", "high"],
        "change_reason": "启用逐提交的代码规范检查及独立审查。",
        **overrides,
    }


def setup(w):
    p, repo, task, log, provider = checkpoint(w)
    configure(w, p, repo, required_kinds=["ci"], environments=[])
    w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/code-audit-policy",
        who=w.root,
        body=policy_body(),
    )
    return p, repo, task, log, provider


def page(w, p, task, who=None):
    return w.ok("GET", f"/api/projects/{p}/code-audits?task_id={task['id']}", who=who or w.root)


def claim(w, p, job, who=None, **overrides):
    return w.ok(
        "POST",
        f"/api/projects/{p}/code-audits/{job['id']}/claim",
        who=who or w.root,
        body={
            "expected_version": job["version"],
            "instance_id": str(uuid.uuid4()),
            **({} if who else {"override_reason": "隔离测试由超管明确记录自审例外。"}),
            **overrides,
        },
    )


def content(job, **overrides):
    base_checks = {"scope": "pass", "security": "pass", "correctness": "pass"}
    if overrides.get("rule_checks"):
        overrides["rule_checks"] = {**base_checks, **overrides["rule_checks"]}
    return {
        "conclusion": "pass",
        "summary": "已核对接口分层和任务范围，没有发现阻断问题。",
        "checkout_commit": job["git_commit"],
        "source_basis": "exact_checkout",
        "reviewed_paths": ["src/login.ts"],
        "rule_checks": {**base_checks, "layers": "pass"},
        "findings": [],
        "gaps": [],
        "verification": "合成源码审查结果；CI使用独立提供方模拟回执。",
        **overrides,
    }


def finish(w, p, job, who=None, **overrides):
    return w.call(
        "POST",
        f"/api/projects/{p}/code-audits/{job['id']}/complete",
        who=who or w.root,
        body={
            "expected_version": job["version"],
            "instance_id": job["instance_id"],
            "lease_epoch": job["lease_epoch"],
            "source_hash": job["source_hash"],
            "content": content(job, **overrides),
        },
    )


def reviewer(w, p, repo, login="reviewer-one", caps=None):
    member = w.member(p, login=login)
    return w.start(
        member,
        grant(
            w, member, p, [(repo, ["code:read"])], caps=caps or ["project:read", "tasks:review", "logs:read"]
        ),
    )


def test_audit_auto_queue_ci_and_independent_report_gate(world):
    w = world
    p, repo, task, log, provider = setup(w)
    data = page(w, p, task)
    assert data["gate"]["required"] and not data["gate"]["ready"]
    assert len(data["items"]) == 1 and data["items"][0]["current"]
    who = reviewer(w, p, repo)
    job = claim(w, p, data["items"][0], who)
    audit_input = w.ok(
        "GET",
        f"/api/projects/{p}/code-audits/{job['id']}/input?instance_id={job['instance_id']}&lease_epoch=1",
        who=who,
    )
    assert audit_input["policy"]["rules"][0]["id"] == "layers"
    response = finish(w, p, job, who)
    assert response.status_code == 200, response.text
    report = response.json()["reports"][0]
    assert report["passed"] and not response.json()["self_review"]
    assert not page(w, p, task)["gate"]["ready"]  # prose does not replace verified CI
    verify(w, p, task, log, "ci")
    assert page(w, p, task)["gate"]["ready"]
    task = gate(w, p, task, "review", who=who)
    assert task["status"] == "testing"


def test_audit_requires_ci_configuration_and_explicit_superadmin_self_review(world):
    w = world
    p, repo, task, _, _ = checkpoint(w)
    path = f"/api/projects/{p}/repositories/{repo['id']}/code-audit-policy"
    assert (
        w.call("PUT", path, who=w.root, body=policy_body()).json()["code"] == "CODE_AUDIT_CI_POLICY_REQUIRED"
    )
    configure(w, p, repo, required_kinds=["ci"], environments=[])
    w.ok("PUT", path, who=w.root, body=policy_body())
    queued = page(w, p, task)["items"][0]
    response = w.call(
        "POST",
        f"/api/projects/{p}/code-audits/{queued['id']}/claim",
        who=w.root,
        body={"expected_version": 1, "instance_id": str(uuid.uuid4())},
    )
    assert response.json()["code"] == "CODE_AUDIT_INDEPENDENT_REVIEW_REQUIRED"
    job = claim(w, p, queued)
    done = finish(w, p, job)
    assert done.status_code == 200, done.text
    assert done.json()["self_review"] and done.json()["override_reason"]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"gaps": ["缺少调用方代码，无法核验兼容性。"]}, "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"),
        ({"source_basis": "archived_worktree"}, "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"),
        ({"reviewed_paths": []}, "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"),
        ({"checkout_commit": "f" * 40}, "CODE_AUDIT_SOURCE_MISMATCH"),
        ({"rule_checks": {}}, "CODE_AUDIT_RULE_COVERAGE_REQUIRED"),
        ({"rule_checks": {"layers": "unverified"}}, "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"),
    ],
)
def test_audit_cannot_pass_missing_coverage_or_wrong_source(world, changes, code):
    w = world
    p, _, task, _, _ = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    response = finish(w, p, job, **changes)
    assert response.json()["code"] == code, response.text
    assert not page(w, p, task)["gate"]["ready"]


def test_audit_findings_retry_immutability_and_repair_history(world):
    w = world
    p, repo, task, log, _ = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    finding = {
        "severity": "high",
        "rule_id": "layers",
        "path": "src/login.ts",
        "line": 4,
        "title": "越过权限边界",
        "evidence": "界面读取了不属于本项目的记录。",
        "recommendation": "通过服务端获准接口重新核验权限。",
    }
    assert finish(w, p, job, findings=[finding]).json()["code"] == "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"
    result = finish(w, p, job, conclusion="fail", findings=[finding], rule_checks={"layers": "fail"})
    assert result.status_code == 200, result.text
    assert not result.json()["reports"][0]["passed"]
    with w.client.app.state.sessions() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE code_audit_reports SET passed=true WHERE job_id=:j"), {"j": job["id"]})
    retried = w.ok(
        "POST",
        f"/api/projects/{p}/code-audits/{job['id']}/retry",
        who=w.root,
        body={
            "expected_version": result.json()["version"],
            "reason": "原报告触发条件需要重新核验，保留复审依据。",
        },
    )
    job2 = claim(w, p, retried)
    result2 = finish(w, p, job2)
    assert result2.status_code == 200, result2.text
    assert [r["passed"] for r in result2.json()["reports"]] == [True, False]
    assert result2.json()["lease_epoch"] == 2
    verify(w, p, task, log, "ci")
    assert page(w, p, task)["gate"]["ready"]
    assert any(e["kind"] == "retry" for e in result2.json()["events"])


def test_audit_policy_and_ci_policy_changes_invalidate_reports(world):
    w = world
    p, repo, task, log, provider = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    assert finish(w, p, job).status_code == 200
    verify(w, p, task, log, "ci")
    assert page(w, p, task)["gate"]["ready"]
    w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/code-audit-policy",
        who=w.root,
        body=policy_body(expected_version=1, change_reason="增加接口错误处理规范并重新核验当前提交。"),
    )
    items = page(w, p, task)["items"]
    assert len(items) == 2 and sum(i["current"] for i in items) == 1
    assert not next(i for i in items if i["id"] == job["id"])["current"]
    assert not page(w, p, task)["gate"]["ready"]
    configure(w, p, repo, expected_version=1, required_kinds=["ci"], environments=[])
    assert len(page(w, p, task)["items"]) == 3


def test_audit_latest_failed_ci_overrides_previous_success(world):
    w = world
    p, _, task, log, provider = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    assert finish(w, p, job).status_code == 200
    verify(w, p, task, log, "ci")
    assert page(w, p, task)["gate"]["ready"]
    provider.ci["conclusion"] = "failure"
    verify(w, p, task, log, "ci")
    assert not page(w, p, task)["gate"]["ready"]


def test_audit_scope_repository_and_device_are_enforced(world):
    w = world
    p, repo, task, _, _ = setup(w)
    other = register(w, p, "pc")
    who = reviewer(w, p, other)
    job = page(w, p, task)["items"][0]
    assert w.call("GET", f"/api/projects/{p}/code-audits/{job['id']}", who=who).status_code == 404
    assert (
        w.call(
            "GET", f"/api/projects/{p}/code-audits/{job['id']}?repository_id={other['id']}", who=w.root
        ).status_code
        == 404
    )
    assert (
        w.call(
            "GET", f"/api/projects/{p}/code-audits/{job['id']}?task_id={uuid.uuid4()}", who=w.root
        ).status_code
        == 404
    )
    who = reviewer(w, p, repo, login="correct-reviewer")
    job = claim(w, p, job, who)
    assert finish(w, p, job).json()["code"] == "CODE_AUDIT_LEASE_EXPIRED"


def test_audit_two_reviewers_cannot_claim_same_lease(world):
    w = world
    p, repo, task, _, _ = setup(w)
    a, b = reviewer(w, p, repo), reviewer(w, p, repo, login="reviewer-two")
    job = page(w, p, task)["items"][0]
    barrier = Barrier(2)

    def compete(who):
        barrier.wait(timeout=5)
        return w.call(
            "POST",
            f"/api/projects/{p}/code-audits/{job['id']}/claim",
            who=who,
            body={"expected_version": 1, "instance_id": str(uuid.uuid4())},
        )

    with ThreadPoolExecutor(2) as pool:
        responses = [f.result(timeout=15) for f in [pool.submit(compete, a), pool.submit(compete, b)]]
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_database_gate_and_report_forgery_are_blocked(world):
    w = world
    p, _, task, _, _ = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    with w.client.app.state.sessions() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        with pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE tasks SET status='testing',version=version+1 WHERE id=:t"), {"t": task["id"]}
            )
    with w.client.app.state.sessions() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        db.add(
            AuditReport(
                job_id=uuid.UUID(job["id"]),
                lease_epoch=1,
                author_id=uuid.UUID(w.root.user_id),
                device_id=uuid.UUID(w.root.enrollment["device_id"]),
                instance_id=uuid.UUID(job["instance_id"]),
                source_hash=job["source_hash"],
                content=content(job, gaps=["未检查源码。"]),
                passed=True,
                receipt_hash="f" * 64,
            )
        )
        with pytest.raises(DBAPIError):
            db.flush()
    with w.owner.begin() as db:
        # An expired owner-fixture lease cannot submit even through the authorized API.
        db.execute(text("ALTER TABLE code_audits DISABLE TRIGGER code_audit_update_guard"))
        db.execute(
            text("UPDATE code_audits SET lease_until=:until,version=version+1 WHERE id=:j"),
            {"j": job["id"], "until": now() - timedelta(minutes=1)},
        )
        db.execute(text("ALTER TABLE code_audits ENABLE TRIGGER code_audit_update_guard"))
    assert finish(w, p, job).json()["code"] == "CODE_AUDIT_LEASE_EXPIRED"
    with w.owner() as db:
        assert db.scalar(select(CodeAudit).where(CodeAudit.id == uuid.UUID(job["id"]))).state == "running"


def test_audit_frozen_input_is_paged_without_losing_rules(world):
    w = world
    p, _, task, _, _ = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    base = (
        f"/api/projects/{p}/code-audits/{job['id']}/input"
        f"?instance_id={job['instance_id']}&lease_epoch=1&limit=1000"
    )
    chunks, offset = [], 0
    while True:
        data = w.ok("GET", base + f"&offset={offset}", who=w.root)
        assert len(data["input_fragment"]) <= 1000 and data["source_hash"] == job["source_hash"]
        chunks.append(data["input_fragment"])
        if not data["has_more"]:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    complete_input = json.loads("".join(chunks))
    assert complete_input["policy"]["rules"][0]["id"] == "layers"
    assert {r["id"] for r in complete_input["base_rules"]} == {"scope", "security", "correctness"}
    assert complete_input["project_rules"]["rules"] and len(chunks) > 1


def test_audit_read_source_preserves_archive_trust_boundary(world):
    from test_work_archives import compress, setup_archive, upload

    from agent_hub.git_gateway import branch_for
    from agent_hub.models import CodeDelivery

    w = world
    p, repo, task, run, _, log, value, snapshot_path = setup_archive(w)
    assert upload(w, snapshot_path, compress(value)).status_code == 200
    configure(w, p, repo, required_kinds=["ci"], environments=[])
    w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/code-audit-policy",
        who=w.root,
        body=policy_body(),
    )
    with w.owner.begin() as db:
        db.add(
            CodeDelivery(
                project_id=uuid.UUID(p),
                repository_id=uuid.UUID(repo["id"]),
                task_id=uuid.UUID(task["id"]),
                worklog_id=uuid.UUID(log["id"]),
                run_id=uuid.UUID(run["run"]["id"]),
                author_id=uuid.UUID(w.root.user_id),
                device_id=uuid.UUID(w.root.enrollment["device_id"]),
                git_commit=log["git_commit"],
                git_ref=branch_for(task["id"], run["run"]["id"]),
                receipt_hash="e" * 64,
            )
        )
    job = claim(w, p, page(w, p, task)["items"][0])
    path = (
        f"/api/projects/{p}/code-audits/{job['id']}/source"
        f"?instance_id={job['instance_id']}&lease_epoch=1&path=src/login.ts"
    )
    before = w.ok("GET", path + "&side=before", who=w.root)
    after = w.ok("GET", path + "&side=after", who=w.root)
    assert before["text"] == "original\n" and after["text"] == "uncommitted change\n"
    assert after["source_basis"] == "archived_worktree"
    assert (
        finish(w, p, job, source_basis="archived_worktree").json()["code"]
        == "CODE_AUDIT_INCOMPLETE_CANNOT_PASS"
    )
    other = reviewer(w, p, repo)
    assert w.call("GET", path, who=other).json()["code"] == "CODE_AUDIT_LEASE_EXPIRED"


def test_audit_delivery_trigger_deduplicates_and_new_commit_invalidates_previous(world):
    from test_tasks import claim as claim_development
    from test_tasks import lease, log_body

    from agent_hub.git_gateway import branch_for
    from agent_hub.models import CodeDelivery

    w = world
    p, repo, task, log, _ = setup(w)
    job = claim(w, p, page(w, p, task)["items"][0])
    assert finish(w, p, job).status_code == 200
    verify(w, p, task, log, "ci")
    request_path = f"/api/projects/{p}/tasks/{task['id']}/code-audits"
    body = {"expected_version": task["version"], "reason": "核对相同来源重复请求不会创建重复审计。"}
    w.ok("POST", request_path, who=w.root, body=body)
    w.ok("POST", request_path, who=w.root, body=body)
    assert len(page(w, p, task)["items"]) == 1
    task = gate(
        w, p, task, "review", approved=False, override_reason="隔离环境退回当前提交并创建修复检查点。"
    )
    run = claim_development(w, p, task)
    base = f"/api/projects/{p}/tasks/{task['id']}"
    new_log = w.ok("POST", base + "/worklogs", who=w.root, body=log_body(run, git_commit="b" * 40))
    with w.owner.begin() as db:
        db.add(
            CodeDelivery(
                project_id=uuid.UUID(p),
                repository_id=uuid.UUID(repo["id"]),
                task_id=uuid.UUID(task["id"]),
                worklog_id=uuid.UUID(new_log["id"]),
                run_id=uuid.UUID(run["run"]["id"]),
                author_id=uuid.UUID(w.root.user_id),
                device_id=uuid.UUID(w.root.enrollment["device_id"]),
                git_commit="b" * 40,
                git_ref=branch_for(task["id"], run["run"]["id"]),
                receipt_hash="e" * 64,
            )
        )
    task = w.ok(
        "POST",
        base + "/submit",
        who=w.root,
        body={**lease(run), "expected_version": run["task"]["version"], "worklog_id": new_log["id"]},
    )
    data = page(w, p, task)
    assert not data["gate"]["ready"] and len(data["items"]) == 2
    assert not next(i for i in data["items"] if i["id"] == job["id"])["current"]
