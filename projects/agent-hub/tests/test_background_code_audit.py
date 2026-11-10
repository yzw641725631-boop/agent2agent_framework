"""Actual PostgreSQL leases/authorization; GitHub and model responses are isolated fixtures."""

import base64
import hashlib

import httpx
import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import select, text
from test_code_audit import content, page, reviewer, setup
from test_server_generation import enable, provider_config, worker  # noqa: F401

# ruff: noqa: F811
from agent_hub.audit_source import AuditSource
from agent_hub.code_audit import AuditReport
from agent_hub.generation import GenerationReceipt
from agent_hub.security import HubError
from agent_hub.server_generation import Call

BASE, HEAD = "b" * 40, "a" * 40


class Source:
    def __init__(self):
        self.count = 0
        self.gaps = []

    def read(self, url, base, head, token, **kwargs):
        self.count += 1
        assert base == BASE and head == HEAD
        return {
            "files": [{"path": "src/login.ts", "before": "return false", "after": "return true"}],
            "gaps": self.gaps,
        }, {
            "git_commit": HEAD,
            "base_commit": BASE,
            "changed_paths": ["src/login.ts"],
            "gaps": self.gaps,
            "manifest_hash": "d" * 64,
        }


def arrange(w, runner, fake):
    p, repo, task, log, _ = setup(w)
    # Synthetic unmanaged delivery fixture needs an explicit observed baseline.
    with w.owner.begin() as db:
        db.execute(text("CREATE POLICY fixture_owner_update ON worklogs FOR UPDATE TO hub_owner "
                        "USING(true) WITH CHECK(true)"))
        result = db.execute(
            text("UPDATE worklogs SET change_evidence=:e WHERE id=:id"),
            {"id": log["id"], "e": Jsonb({"base_commit": BASE})},
        )
        assert result.rowcount == 1
        db.execute(text("DROP POLICY fixture_owner_update ON worklogs"))
    who = reviewer(w, p, repo)
    provider_config(w)
    enable(
        w,
        p,
        repo,
        distillation=False,
        code_audit=True,
        reviewer_user_id=who.user_id,
        reviewer_device_id=who.enrollment["device_id"],
    )
    job = page(w, p, task)["items"][0]
    fake.content = content(job, source_basis="exact_git_objects")
    runner.audit_source = Source()
    return p, repo, task, who


def test_background_audit_uses_independent_readonly_actor_and_preserves_receipt(world, worker):
    runner, fake = worker
    p, _, task, who = arrange(world, runner, fake)
    assert runner.run_once() and fake.count == 1 and runner.audit_source.count == 1
    assert not runner.run_once()
    result = page(world, p, task)["items"][0]
    assert result["state"] == "completed" and result["reviewer_id"] == who.user_id
    with world.owner() as db:
        assert db.scalar(select(AuditReport)).passed
        assert db.scalar(select(GenerationReceipt)).code_audit_id
        call = db.scalar(select(Call))
        assert call.state == "shared" and call.encrypted_result is None
        assert call.source_receipt["git_commit"] == HEAD


def test_source_gaps_force_incomplete_even_if_provider_claims_pass(world, worker):
    runner, fake = worker
    p, _, task, _ = arrange(world, runner, fake)
    runner.audit_source.gaps = ["Binary source unavailable"]
    assert runner.run_once() and fake.count == 1
    with world.owner() as db:
        report = db.scalar(select(AuditReport))
        assert not report.passed and report.content["conclusion"] == "incomplete"
        assert "Binary source unavailable" in report.content["gaps"]
    assert not page(world, p, task)["gate"]["ready"]


def test_revoked_background_reviewer_cannot_submit_and_result_is_not_recalled(world, worker):
    runner, fake = worker
    _, _, _, who = arrange(world, runner, fake)
    fake.callback = lambda _: world.ok(
        "POST", f"/api/admin/users/{who.user_id}/status", who=world.root, body={"status": "suspended"}
    )
    assert runner.run_once() and fake.count == 1
    with world.owner() as db:
        assert not db.scalar(select(AuditReport))
        assert db.scalar(select(Call)).state == "blocked"
    assert not runner.run_once() and fake.count == 1


def test_source_reader_verifies_blobs_and_rejects_incomplete_diff():
    raw = b"export const ok = true;\n"
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def handle(request):
        assert request.url.host == "api.github.com" and "authorization" not in request.headers
        if "/compare/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "base_commit": {"sha": BASE},
                    "merge_base_commit": {"sha": BASE},
                    "status": "ahead",
                    "files": [{"filename": "src/login.ts", "status": "added"}],
                },
            )
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": HEAD})
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "src/login.ts",
                "encoding": "base64",
                "size": len(raw),
                "sha": sha,
                "content": base64.b64encode(raw).decode(),
            },
        )

    reader = AuditSource(transport=httpx.MockTransport(handle))
    bundle, receipt = reader.read("https://github.com/example/web", BASE, HEAD, None)
    assert bundle["files"][0]["after"] == raw.decode() and receipt["manifest"][0]["blobs"]["after"] == sha
    assert not receipt["repository_code_executed"]
    reader = AuditSource(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"files": []})))
    with pytest.raises(HubError) as error:
        reader.read("https://github.com/example/web", BASE, HEAD, None)
    assert error.value.code == "CODE_AUDIT_COMPLETE_DIFF_REQUIRED"
