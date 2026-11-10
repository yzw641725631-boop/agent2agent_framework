"""Real PostgreSQL authorization/leases/budgets with an isolated provider response."""

# ruff: noqa: F811
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from test_distillation import fixture, jobs
from test_memory import extractor  # noqa: F401

from agent_hub.generation import GenerationReceipt
from agent_hub.generation_provider import Provider, endpoint
from agent_hub.generation_worker import GenerationWorker
from agent_hub.security import HubError, now
from agent_hub.server_generation import Call

CONFIG = {
    "expected_version": 0,
    "endpoint": "http://generation-test:8767/v1/chat/completions",
    "model": "isolated-test-model",
    "api_key": "isolated-provider-key-for-tests",
    "daily_token_limit": 200000,
    "daily_call_limit": 10,
    "reason": "Configure an isolated synthetic provider for integration verification",
}


def provider_config(w, **values):
    return w.ok("PUT", "/api/admin/generation", who=w.root, body={**CONFIG, **values})


def enable(w, p, repo, **values):
    return w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/generation",
        who=w.root,
        body={
            "expected_version": 0,
            "enabled": True,
            "distillation": True,
            "guides": False,
            "reason": "Authorize central generation only for this repository",
            **values,
        },
    )


def expire_job(db, job_id):
    # Clock advance in the isolated fixture only. Normal callers cannot alter leases.
    db.execute(text("ALTER TABLE distillation_jobs DISABLE TRIGGER distillation_transition"))
    db.execute(
        text("UPDATE distillation_jobs SET lease_until=:expired WHERE id=:id"),
        {"expired": now() - timedelta(seconds=1), "id": job_id},
    )
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db.execute(text("ALTER TABLE distillation_jobs ENABLE TRIGGER distillation_transition"))


class Fake:
    def __init__(self):
        self.count = 0
        self.callback = None
        self.content = {
            "outcome": "no_new_knowledge",
            "candidates": [],
            "reason": "The frozen sources do not add a reusable conclusion beyond existing knowledge",
        }

    def handle(self, request):
        self.count += 1
        assert request.headers["authorization"] == "Bearer " + CONFIG["api_key"]
        assert request.headers["idempotency-key"]
        payload = json.loads(request.content)
        assert not payload.get("tools") and payload["stream"] is False
        assert payload["response_format"] == {"type": "json_object"}
        if self.callback:
            self.callback(payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.content)}}],
                "usage": {"prompt_tokens": 321, "completion_tokens": 42},
            },
        )


@pytest.fixture
def worker(database):
    cfg, owner = database
    engine = create_engine(owner.kw["bind"].url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def role(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE hub_generation")
        connection.commit()

    factory = sessionmaker(engine, expire_on_commit=False)
    fake = Fake()
    value = GenerationWorker(
        cfg, factory=factory, provider=Provider(transport=httpx.MockTransport(fake.handle), test=True)
    )
    yield value, fake
    engine.dispose()


def test_central_worker_runs_without_desktop_session_and_has_shared_provenance(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    configured = provider_config(w)
    assert configured["execution_location"] == "server" and configured["api_key_set"]
    assert CONFIG["api_key"] not in json.dumps(configured)
    enable(w, p, repo)
    runner, fake = worker
    assert runner.run_once() and fake.count == 1
    assert not runner.run_once() and fake.count == 1
    assert jobs(w, p)["items"][0]["state"] == "succeeded"
    calls = w.ok("GET", f"/api/projects/{p}/generation-calls", who=w.root)
    assert calls["items"][0]["state"] == "shared" and calls["items"][0]["input_tokens"] == 321
    assert CONFIG["api_key"] not in json.dumps(calls)
    with w.owner() as db:
        assert db.scalar(select(GenerationReceipt)).model == CONFIG["model"]
        call = db.scalar(select(Call))
        assert call.encrypted_result is None and call.output_tokens == 42


def test_disabled_repository_and_unconfigured_service_never_call_a_provider(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    runner, fake = worker
    assert not runner.run_once()
    provider_config(w)
    assert not runner.run_once()
    enable(w, p, repo, enabled=False)
    assert not runner.run_once() and fake.count == 0


def test_revocation_during_inference_blocks_output_and_does_not_hold_project_lock(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    # This HTTP request needs the same user/project locks; it would block if inference held them.
    fake.callback = lambda _: enable(w, p, repo, expected_version=1, enabled=False)
    assert runner.run_once() and fake.count == 1
    with w.owner() as db:
        call = db.scalar(select(Call))
        assert call.state == "blocked" and call.error_code == "GENERATION_AUTHORIZATION_CHANGED"
        assert call.encrypted_result and "no_new_knowledge" not in call.encrypted_result
        assert not db.scalar(select(GenerationReceipt))
    assert not runner.run_once() and fake.count == 1


def test_daily_reservation_is_enforced_before_any_provider_request(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w, daily_token_limit=1000)
    enable(w, p, repo)
    runner, fake = worker
    assert not runner.run_once() and fake.count == 0
    assert jobs(w, p)["items"][0]["state"] == "queued"
    with w.owner() as db:
        assert not db.scalar(select(Call))


def test_provider_timeout_is_uncertain_and_never_repeats_automatically(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker

    def fail(_):
        raise httpx.ReadTimeout("Private provider body must not escape")

    fake.callback = fail
    assert runner.run_once() and fake.count == 1
    assert not runner.run_once() and fake.count == 1
    with w.owner() as db:
        call = db.scalar(select(Call))
        assert call.state == "uncertain" and call.reserved_tokens > 0
        assert call.error_code == "GENERATION_OUTCOME_UNCERTAIN"


def test_invalid_model_output_cannot_complete_a_job(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    fake.content = {
        "outcome": "proposed",
        "candidates": [],
        "reason": "This invalid output has no candidates",
    }
    assert runner.run_once()
    assert jobs(w, p)["items"][0]["state"] == "running"
    with w.owner() as db:
        assert db.scalar(select(Call)).state == "blocked"
        assert not db.scalar(select(GenerationReceipt))


def test_only_root_can_configure_and_plaintext_is_absent_from_database(world, extractor):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    member = w.member(p)
    assert w.call("GET", "/api/admin/generation", who=member).status_code == 403
    assert w.call("PUT", "/api/admin/generation", who=member, body=CONFIG).status_code == 403
    with w.owner() as db:
        content = db.scalar(text("SELECT encrypted_value FROM generation_configuration"))
        assert CONFIG["api_key"] not in content
        assert CONFIG["api_key"] not in json.dumps(
            db.execute(text("SELECT result FROM idempotency_records")).all(), default=str
        )


def test_multiple_workers_do_not_duplicate_the_paid_request(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = list(pool.map(lambda _: runner.run_once(), range(2)))
    assert sum(completed) >= 1 and fake.count == 1
    assert jobs(w, p)["items"][0]["generation"]["execution"] == "server-background"


def test_saved_result_survives_restart_without_another_provider_request(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    task = runner.reserve()
    result = runner.provider.invoke(task["config"], task["payload"], task["call_id"])
    with w.owner.begin() as db:
        row = db.get(Call, task["call_id"])
        row.state, row.encrypted_result = "ready", runner.config.seal(result)
        row.input_tokens, row.output_tokens = result["input_tokens"], result["output_tokens"]
    assert runner.run_once() and fake.count == 1
    assert jobs(w, p)["items"][0]["automatic_model_execution"]
    usage = w.ok("GET", f"/api/projects/{p}/usage", who=w.root)["server_generation"]
    assert usage["calls"] == 1 and usage["provider_input_tokens"] == 321


def test_root_can_resume_saved_output_under_renewed_authority_without_extra_payment(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    fake.callback = lambda _: enable(w, p, repo, expected_version=1, enabled=False)
    assert runner.run_once()
    enable(w, p, repo, expected_version=2, enabled=True)
    with w.owner.begin() as db:
        row = db.scalar(select(Call))
        call_id = row.id
        expire_job(db, row.job_id)
    fake.callback = None
    w.ok(
        "POST",
        f"/api/projects/{p}/generation-calls/{call_id}/retry",
        who=w.root,
        body={"reason": "Revalidate the restored repository authority and reuse the saved response"},
    )
    assert runner.run_once() and fake.count == 1
    assert jobs(w, p)["items"][0]["state"] == "succeeded"


def test_uncertain_paid_retry_requires_root_approval_and_preserves_prior_reservation(
    world, extractor, worker
):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker
    fake.callback = lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("Synthetic timeout"))
    assert runner.run_once()
    with w.owner() as db:
        old = db.scalar(select(Call))
        call_id, job_id = old.id, old.job_id
    path = f"/api/projects/{p}/generation-calls/{call_id}/retry"
    assert (
        w.call(
            "POST",
            path,
            who=w.root,
            body={"reason": "Require an explicit decision for a new billable request"},
        ).json()["code"]
        == "GENERATION_PAID_RETRY_APPROVAL_REQUIRED"
    )
    w.ok(
        "POST",
        path,
        who=w.root,
        body={
            "allow_new_provider_call": True,
            "reason": "Explicitly authorize a new request despite the previous unknown billing",
        },
    )
    assert not runner.run_once() and fake.count == 1
    with w.owner.begin() as db:
        assert db.get(Call, call_id).state == "abandoned"
        expire_job(db, job_id)
    fake.callback = None
    assert runner.run_once() and fake.count == 2
    with w.owner() as db:
        calls = list(db.scalars(select(Call)))
        assert len(calls) == 2 and all(row.reserved_tokens > 0 for row in calls)


def test_background_guide_still_requires_independent_review(world, worker):
    from test_guides import CONTENT
    from test_repositories import register, task
    from test_tasks import claim, submit

    w = world
    p = w.project()
    repo = register(w, p)
    run = claim(w, p, task(w, p, repo, documentation={"kind": "user", "module": "登录界面说明"}))
    submit(w, p, run)
    provider_config(w)
    enable(w, p, repo, distillation=False, guides=True)
    runner, fake = worker
    fake.content = CONTENT
    assert runner.run_once() and fake.count == 1
    guide = w.ok("GET", f"/api/projects/{p}/guides", who=w.root)["items"][0]
    assert guide["state"] == "review" and guide["generation"]["execution"] == "server-background"


@pytest.mark.parametrize("revoke", [False, True])
def test_background_member_guide_uses_current_repository_write_grant_and_rechecks_membership(
    world, worker, revoke
):
    from test_guides import CONTENT
    from test_repositories import grant, register, task
    from test_tasks import claim, submit

    w = world
    p = w.project()
    repo = register(w, p)
    member = w.member(p, "guide-contributor")
    w.start(member, grant(w, member, p, [(repo, ["code:read", "code:write"])]))
    run = claim(w, p, task(w, p, repo, documentation={"kind": "user", "module": "成员登录说明"}), member)
    submit(w, p, run, member)
    # Renewal replaces the old grant. Generation must use the currently approved one.
    w.start(member, grant(w, member, p, [(repo, ["code:read", "code:write"])]))
    provider_config(w)
    enable(w, p, repo, distillation=False, guides=True)
    runner, fake = worker
    fake.content = CONTENT
    if revoke:
        fake.callback = lambda _: w.ok(
            "POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={}
        )
    assert runner.run_once() and fake.count == 1
    with w.owner() as db:
        call = db.scalar(select(Call))
        assert str(call.user_id) == member.user_id
        assert str(call.device_id) == member.enrollment["device_id"]
        assert str(call.instance_id) == run["run"]["instance_id"]
        assert call.state == ("blocked" if revoke else "shared")
        assert bool(db.scalar(select(GenerationReceipt))) is not revoke


def test_revoked_authorizing_device_blocks_saved_results(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    runner, fake = worker

    def revoke(_):
        with w.owner.begin() as db:
            db.execute(
                text("UPDATE devices SET status='revoked' WHERE id=:id"),
                {"id": w.root.enrollment["device_id"]},
            )

    fake.callback = revoke
    assert runner.run_once() and fake.count == 1
    with w.owner() as db:
        call = db.scalar(select(Call))
        assert call.state == "blocked" and call.error_code == "GENERATION_AUTHORIZATION_REVOKED"
        assert not db.scalar(select(GenerationReceipt))


def test_project_budget_blocks_central_call_before_payment(world, extractor, worker):
    w = world
    p, repo = fixture(w, extractor)
    provider_config(w)
    enable(w, p, repo)
    w.ok(
        "PUT",
        f"/api/projects/{p}/usage/budget",
        who=w.root,
        body={
            "expected_version": 0,
            "mode": "pause-managed-work",
            "monthly_reported_token_limit": 10,
            "change_reason": "Verify conservative reservation is checked before contacting any provider",
        },
    )
    runner, fake = worker
    assert not runner.run_once() and fake.count == 0


def test_compatible_provider_actual_http_roundtrip_does_not_use_environment_proxy(monkeypatch):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(
                {
                    "path": self.path,
                    "auth": self.headers["Authorization"],
                    "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            content = json.dumps(
                {
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8767), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resolve = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: resolve(
            "127.0.0.1" if host == "generation-test" else host, *args, **kwargs
        ),
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    try:
        result = Provider(test=True).invoke(
            {**CONFIG, "timeout_seconds": 5}, b'{"model":"test"}', "http-fixture"
        )
        assert result["value"] == {"ok": True} and result["input_tokens"] == 12
        assert captured == [
            {
                "path": "/v1/chat/completions",
                "auth": "Bearer " + CONFIG["api_key"],
                "payload": {"model": "test"},
            }
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "status,content,code",
    [
        (302, b"", "GENERATION_PROVIDER_REJECTED"),
        (401, b"secret response must stay private", "GENERATION_PROVIDER_REJECTED"),
        (200, b"x" * 262145, "GENERATION_RESPONSE_TOO_LARGE"),
        (
            200,
            b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}',
            "GENERATION_OUTCOME_UNCERTAIN",
        ),
        (
            200,
            b'{"choices":[{"finish_reason":"stop","message":{"content":"[]"}}]}',
            "GENERATION_OUTCOME_UNCERTAIN",
        ),
    ],
)
def test_provider_refusals_redirects_truncation_and_large_results_do_not_escape(status, content, code):
    provider = Provider(
        test=True, transport=httpx.MockTransport(lambda _: httpx.Response(status, content=content))
    )
    with pytest.raises(HubError) as error:
        provider.invoke({**CONFIG, "timeout_seconds": 5}, b"{}", "provider-fixture")
    assert error.value.code == code and CONFIG["api_key"] not in str(error.value)


def test_production_dns_must_resolve_entirely_to_public_addresses(monkeypatch):
    from agent_hub.generation_provider import public_addresses

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(HubError, match="GENERATION_ENDPOINT_INVALID"):
        public_addresses("example.org")


@pytest.mark.parametrize(
    "value",
    [
        "http://example.org/v1/chat/completions",
        "https://127.0.0.1/v1/chat/completions",
        "https://169.254.169.254/v1/chat/completions",
        "https://example.org:8443/v1/chat/completions",
        "https://user:secret@example.org/v1/chat/completions",
        "https://example.org/v1/chat/completions?secret=x",
    ],
)
def test_production_provider_rejects_unapproved_destinations(value):
    from agent_hub.security import HubError

    with pytest.raises(HubError):
        endpoint(value)
