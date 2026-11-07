"""Regression checks for admission, scoped coordination and post-commit mail."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from conftest import PASSWORD, DeviceKey, World
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agent_hub import api, progress, security
from agent_hub.api import create_app
from agent_hub.mail_outbox import enqueue
from agent_hub.models import Approval, Audit, EmailChallenge, Outbox, RateBucket, User
from agent_hub.security import HubError, now, payload_hash, rate_limit


def test_random_logins_and_missing_proofs_do_not_create_account_buckets(world):
    w = world
    key = DeviceKey()
    with w.owner() as db:
        initial = db.scalar(select(func.count()).select_from(RateBucket))
    for index in range(8):
        response = w.call(
            "POST",
            "/api/auth/enrollments",
            key=key,
            body={
                "login": f"nonexistent-{index}",
                "password": PASSWORD,
                "os": "windows",
                "device_name": "test",
            },
        )
        assert response.json()["code"] == "INVALID_CREDENTIALS"
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(RateBucket)) == initial
        before_audit = db.scalar(select(func.count()).select_from(Audit))
    response = w.call(
        "POST",
        "/api/auth/enrollments",
        body={"login": "root-admin", "password": PASSWORD, "os": "windows", "device_name": "test"},
    )
    assert response.json()["code"] == "DEVICE_PROOF_REQUIRED"
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(Audit)) == before_audit
    secret = w.client.app.state.hub.secret
    with w.owner.begin() as db:
        row = db.get(RateBucket, security.digest(secret, "ip:testclient"))
        row.count = 60
    for index in range(3):
        response = w.call(
            "POST",
            "/api/auth/enrollments",
            key=key,
            body={
                "login": f"rate-denied-{index}",
                "password": PASSWORD,
                "os": "windows",
                "device_name": "test",
            },
        )
        assert response.json()["code"] == "RATE_LIMITED"
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(RateBucket)) == initial
        assert db.get(RateBucket, security.digest(secret, "ip:testclient")).count == 61


def test_expired_rate_buckets_are_cleaned_in_bounded_batches(world):
    w = world
    with w.owner.begin() as db:
        db.add_all(
            RateBucket(key=f"{i:064x}", count=1, resets_at=now() - timedelta(days=1)) for i in range(250)
        )
    state = w.client.app.state
    rate_limit(state.security_sessions, state.hub.secret, "cleanup-test", 10, 60)
    with w.owner() as db:
        assert (
            db.scalar(select(func.count()).select_from(RateBucket).where(RateBucket.resets_at < now())) == 150
        )
    rate_limit(state.security_sessions, state.hub.secret, "cleanup-test", 10, 60)
    with w.owner() as db:
        assert (
            db.scalar(select(func.count()).select_from(RateBucket).where(RateBucket.resets_at < now())) == 50
        )


def test_expired_device_application_can_be_renewed_once_but_denied_cannot(world):
    w = world
    person = w.member(w.project(), approve=False)
    old = person.enrollment["approval"]
    with w.owner.begin() as db:
        db.get(Approval, uuid.UUID(old["id"])).expires_at = now() - timedelta(seconds=1)
    _, renewed = w.enroll("member-one", person.key)
    assert renewed["device_id"] == person.enrollment["device_id"]
    assert renewed["approval"]["id"] != old["id"]
    _, duplicate = w.enroll("member-one", person.key)
    assert duplicate["approval"]["id"] == renewed["approval"]["id"]
    with w.owner() as db:
        assert db.get(Approval, uuid.UUID(old["id"])).status == "expired"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.subject_id == uuid.UUID(person.user_id))
            )
            == 2
        )
    w.decide(renewed["approval"], approve=False)
    response = w.call(
        "POST",
        "/api/auth/enrollments",
        key=person.key,
        body={"login": "member-one", "password": PASSWORD, "device_name": "test", "os": "windows"},
    )
    assert response.json()["code"] == "DEVICE_APPLICATION_DENIED"


def test_expired_approvals_do_not_hide_new_ones_and_pages_are_stable(world):
    w = world
    root_id = uuid.UUID(w.root.user_id)
    with w.owner.begin() as db:
        for index in range(210):
            payload = {"fixture": index}
            db.add(
                Approval(
                    kind="device",
                    requester_id=root_id,
                    subject_id=root_id,
                    payload=payload,
                    payload_hash=payload_hash(payload),
                    created_at=now() - timedelta(days=10),
                    expires_at=now() + timedelta(days=1) if index >= 105 else now() - timedelta(days=1),
                )
            )
    first = w.ok("GET", "/api/admin/approvals?offset=0&limit=100", who=w.root)["items"]
    second = w.ok("GET", "/api/admin/approvals?offset=100&limit=100", who=w.root)["items"]
    assert len(first) == 100 and len(second) == 5
    assert len({a["id"] for a in first + second}) == 105
    assert all(a["payload"]["fixture"] >= 105 for a in first + second)
    old = first[0]
    with w.owner.begin() as db:
        db.get(Approval, uuid.UUID(old["id"])).expires_at = now() - timedelta(seconds=1)
    response = w.call(
        "POST",
        f"/api/admin/approvals/{old['id']}/decision",
        who=w.root,
        body={"approve": True, "expected_version": old["version"], "expected_hash": old["payload_hash"]},
    )
    assert response.json()["code"] == "APPROVAL_NOT_PENDING"
    with w.owner() as db:
        assert db.get(Approval, uuid.UUID(old["id"])).status == "expired"


def test_slow_smtp_holds_no_database_connection_or_identity_lock(world, monkeypatch):
    w = world
    entered, resume = Event(), Event()
    send = w.mail.send_test

    def blocked(*args):
        entered.set()
        assert resume.wait(5)
        return send(*args)

    monkeypatch.setattr(w.mail, "send_test", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(w.call, "POST", "/api/admin/settings/test-email", who=w.root, body={})
        try:
            assert entered.wait(3)
            assert w.client.app.state.sessions.kw["bind"].pool.checkedout() == 0
            # Even another request by the same superadmin can proceed during SMTP.
            assert pool.submit(w.call, "GET", "/api/me", who=w.root).result(2).status_code == 200
        finally:
            resume.set()
        assert delivery.result(3).status_code == 200


def test_failed_mail_is_recorded_without_secrets_and_allows_a_clean_resend(world, monkeypatch):
    w = world
    person = w.member(w.project(), approve=False)
    args = {"key": person.key, "token": person.enrollment["enrollment_token"], "body": {}}
    original = w.mail.send_code

    def failed(*_args):
        raise HubError("MAIL_CONNECTION_FAILED", 503)

    monkeypatch.setattr(w.mail, "send_code", failed)
    response = w.call("POST", "/api/devices/email-challenges", **args)
    # This account has no email. Bind one in the fixture before exercising delivery.
    assert response.json()["code"] == "NO_EMAIL_BOUND"

    with w.owner.begin() as db:
        db.get(User, uuid.UUID(person.user_id)).email = "delivery-fixture@example.org"
    response = w.call("POST", "/api/devices/email-challenges", **args)
    assert response.status_code == 503 and response.json()["code"] == "MAIL_CONNECTION_FAILED"
    with w.owner() as db:
        jobs = list(db.scalars(select(Outbox).where(Outbox.kind == "mail.delivery")))
        assert len(jobs) == 1 and jobs[0].state == "failed"
        assert "sealed" not in jobs[0].payload and "delivery-fixture" not in json.dumps(jobs[0].payload)
        assert db.scalar(select(func.count()).select_from(EmailChallenge)) == 0
    monkeypatch.setattr(w.mail, "send_code", original)
    assert w.call("POST", "/api/devices/email-challenges", **args).status_code == 200


def test_mail_outbox_recovers_interrupted_sender_once_and_cancels_expired_intents(world):
    w = world
    cfg, deliveries = w.client.app.state.hub.settings, w.client.app.state.deliveries
    with w.owner.begin() as db:
        job_id = enqueue(db, cfg, "recover@example.org", code="12345678")
        job = db.get(Outbox, job_id)
        assert "12345678" not in json.dumps(job.payload) and "recover@example.org" not in json.dumps(
            job.payload
        )
        job.created_at = now() - timedelta(minutes=5)
        job.state = "running"
        job.payload = {
            **job.payload,
            "lease_until": (now() - timedelta(seconds=1)).isoformat(),
            "lease_id": str(uuid.uuid4()),
            "attempts": 1,
        }
        expired_id = enqueue(db, cfg, "expired@example.org", code="87654321", expires_at=now())
        db.get(Outbox, expired_id).created_at = now() - timedelta(minutes=5)
    deliveries.recover_once()
    deliveries.recover_once()
    assert len([message for message in w.mail.messages if message["email"] == "recover@example.org"]) == 1
    assert not any(message["email"] == "expired@example.org" for message in w.mail.messages)
    with w.owner() as db:
        assert db.get(Outbox, job_id).state == "sent"
        assert "sealed" not in db.get(Outbox, job_id).payload
        assert db.get(Outbox, expired_id).state == "cancelled"


def test_mail_failure_cannot_delete_a_code_already_consumed_during_delivery(world, monkeypatch):
    w = world
    person = w.member(w.project(), approve=False)
    args = {"key": person.key, "token": person.enrollment["enrollment_token"]}
    with w.owner.begin() as db:
        db.get(User, uuid.UUID(person.user_id)).email = "consumed@example.org"

    def consumed(email, code, _config):
        with w.owner() as db:
            challenge = db.scalar(select(EmailChallenge).where(EmailChallenge.email == email))
            challenge_id = str(challenge.id)
        w.ok(
            "POST",
            "/api/devices/email-verifications",
            **args,
            body={"challenge_id": challenge_id, "code": code},
        )
        # SMTP may fail after the recipient has actually received its message.
        raise HubError("MAIL_CONNECTION_FAILED", 503)

    monkeypatch.setattr(w.mail, "send_code", consumed)
    assert w.call("POST", "/api/devices/email-challenges", body={}, **args).status_code == 503
    with w.owner() as db:
        challenge = db.scalar(select(EmailChallenge).where(EmailChallenge.email == "consumed@example.org"))
        assert challenge is not None and challenge.consumed_at is not None
        assert db.get(User, uuid.UUID(person.user_id)).email_verified_at is not None


def test_an_active_mail_lease_prevents_a_second_sender(world, monkeypatch):
    w = world
    cfg, deliveries = w.client.app.state.hub.settings, w.client.app.state.deliveries
    with w.owner.begin() as db:
        job_id = enqueue(db, cfg, "one-sender@example.org", code="12345678")
    entered, resume = Event(), Event()
    original = w.mail.send_code

    def held(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(w.mail, "send_code", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sender = pool.submit(deliveries.deliver, job_id)
        try:
            assert entered.wait(3)
            pool.submit(deliveries.deliver, job_id).result(2)
        finally:
            resume.set()
        sender.result(3)
    assert len([m for m in w.mail.messages if m["email"] == "one-sender@example.org"]) == 1


def test_small_business_pool_cannot_starve_independent_security_transactions(world, database, monkeypatch):
    w = world
    first, second = w.project(), w.project("second")
    members = [w.member(first), w.member(second, "second-member")]
    for person, project in zip(members, [first, second], strict=True):
        w.start(person, w.grant(person, project, ["project:read", "tasks:write"]))
    engine = create_engine(
        database[0].database_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        hide_parameters=True,
    )
    app = create_app(database[0], factory=sessionmaker(engine, expire_on_commit=False), mailer=w.mail)
    barrier, original = Barrier(2), api.rate_limit

    def simultaneous(factory, secret, scope, *args):
        if scope.startswith("progress:"):
            barrier.wait(3)
        return original(factory, secret, scope, *args)

    monkeypatch.setattr(api, "rate_limit", simultaneous)
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            small = World(client, w.mail, w.owner)
            futures = [
                pool.submit(
                    small.call,
                    "POST",
                    f"/api/projects/{project}/progress",
                    who=person,
                    body={
                        "status": "checkpoint",
                        "summary": "pool fixture",
                    },
                )
                for person, project in zip(members, [first, second], strict=True)
            ]
            responses = [future.result(5) for future in futures]
            assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    finally:
        engine.dispose()


def test_unrelated_project_proceeds_while_revocation_waits_for_an_inflight_write(world, monkeypatch):
    w = world
    first, second = w.project(), w.project("second")
    one, two = w.member(first), w.member(second, "second-member")
    for person, project in [(one, first), (two, second)]:
        w.start(person, w.grant(person, project, ["project:read", "tasks:write"]))
    entered, resume = Event(), Event()
    original = progress.report

    def held(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(progress, "report", held)
    with ThreadPoolExecutor(max_workers=3) as pool:
        write = pool.submit(
            w.call,
            "POST",
            f"/api/projects/{first}/progress",
            who=one,
            body={"status": "checkpoint", "summary": "before revoke"},
        )
        try:
            assert entered.wait(3)
            revoke = pool.submit(
                w.call, "POST", f"/api/projects/{first}/members/{one.user_id}/revoke", who=w.root, body={}
            )
            other = pool.submit(w.call, "GET", f"/api/projects/{second}/context", who=two)
            assert other.result(2).status_code == 200
        finally:
            resume.set()
        assert write.result(3).status_code == 200
        assert revoke.result(3).status_code == 200
    assert w.call("GET", f"/api/projects/{first}/context", who=one).json()["code"] == "PROJECT_ACCESS_REVOKED"


def test_parallel_refresh_detects_replay_and_revokes_the_rotated_family(world):
    w = world
    original = w.root.tokens
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: w.call(
                    "POST", "/api/auth/refresh", key=w.root.key, token=original["refresh_token"], body={}
                ),
                range(2),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 401]
    denied = next(response for response in responses if response.status_code == 401)
    assert denied.json()["code"] == "REFRESH_REPLAY_FAMILY_REVOKED"
    rotated = next(response.json() for response in responses if response.status_code == 200)
    assert w.call("GET", "/api/me", key=w.root.key, token=rotated["access_token"]).status_code == 401


def test_password_work_has_a_memory_bound_and_releases_slots_on_errors(monkeypatch):
    assert security.PASSWORD_SLOTS.acquire(False)
    assert security.PASSWORD_SLOTS.acquire(False)
    try:
        with pytest.raises(HubError) as caught:
            security.verify_password(None, "irrelevant")
        assert caught.value.code == "SERVICE_BUSY"
        with pytest.raises(HubError):
            security.hash_password("irrelevant")
    finally:
        security.PASSWORD_SLOTS.release()
        security.PASSWORD_SLOTS.release()
    assert security.verify_password("invalid-hash", "irrelevant") is False
    assert security.PASSWORD_SLOTS.acquire(False)
    security.PASSWORD_SLOTS.release()
