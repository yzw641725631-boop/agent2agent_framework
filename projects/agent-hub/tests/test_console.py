import json
import uuid
from datetime import timedelta

import pytest
from conftest import PASSWORD, SETUP_TOKEN, DeviceKey, Identity
from sqlalchemy import func, select, update

from agent_hub.models import (
    Audit,
    BootstrapAttempt,
    Device,
    EmailChallenge,
    Idempotency,
    SiteConfiguration,
    User,
)
from agent_hub.security import now

SMTP_PASSWORD = "test-smtp-secret-not-a-real-credential"


def configuration(version=0, **overrides):
    return {
        "expected_version": version,
        "public_url": "https://hub.example.org",
        "smtp_host": "smtp.example.org",
        "smtp_port": 465,
        "smtp_from": "sender@example.org",
        "smtp_username": "sender@example.org",
        "smtp_password": SMTP_PASSWORD,
        "smtp_transport": "tls",
        **overrides,
    }


def setup_start(world, login="new-superadmin"):
    key = DeviceKey()
    args = {"key": key, "setup": SETUP_TOKEN}
    world.ok("PUT", "/api/setup/configuration", body=configuration(), **args)
    attempt = world.ok(
        "POST",
        "/api/setup/challenges",
        body={
            "login": login,
            "password": PASSWORD,
            "email": "root-new@example.org",
            "device_name": "browser workstation",
            "os": "macos",
        },
        **args,
    )
    return args, {"attempt_id": attempt["attempt_id"], "code": world.mail.messages[-1]["code"]}


def test_console_assets_and_security_headers(empty_world):
    response = empty_world.client.get("/")
    assert response.status_code == 200
    assert 'lang="zh-CN"' in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert empty_world.client.get("/assets/api.js").status_code == 200
    assert empty_world.client.get("/assets/app.js").status_code == 200
    assert empty_world.client.get("/assets/../config.py").status_code == 404


def test_setup_requires_secret_and_device_proof(empty_world):
    assert empty_world.client.get("/api/setup/status").json()["setup_required"] is True
    assert empty_world.call("GET", "/api/setup/configuration", key=DeviceKey()).status_code == 401
    invalid = empty_world.call("GET", "/api/setup/configuration", key=DeviceKey(), setup="invalid-token")
    assert invalid.json()["code"] == "INVALID_SETUP_TOKEN"
    assert empty_world.call("GET", "/api/setup/configuration", setup=SETUP_TOKEN).status_code == 401
    with empty_world.owner() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_setup_verifies_email_and_same_device_then_closes_forever(empty_world):
    w = empty_world
    args, verification = setup_start(w)
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        entry = db.get(BootstrapAttempt, uuid.UUID(verification["attempt_id"]))
        assert entry.challenge_digest != verification["code"]
        assert PASSWORD not in entry.password_hash
    other = w.call("POST", "/api/setup/complete", key=DeviceKey(), setup=SETUP_TOKEN, body=verification)
    assert other.json()["code"] == "INVALID_SETUP_CHALLENGE"
    session = w.ok("POST", "/api/setup/complete", body=verification, **args)
    me = w.ok("GET", "/api/me", key=args["key"], token=session["access_token"])
    assert me["user"]["platform_role"] == "superadmin"
    assert me["user"]["email_verified"] is True
    assert w.client.get("/api/setup/status").json()["setup_required"] is False
    assert w.call("GET", "/api/setup/configuration", **args).json()["code"] == "SETUP_ALREADY_COMPLETED"
    assert w.call("POST", "/api/setup/complete", body=verification, **args).status_code == 409
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(func.count()).select_from(BootstrapAttempt)) == 0


def test_numeric_login_can_initialize_and_log_back_in(empty_world):
    w = empty_world
    args, verification = setup_start(w, login="641725631")
    session = w.ok("POST", "/api/setup/complete", body=verification, **args)
    me = w.ok("GET", "/api/me", key=args["key"], token=session["access_token"])
    assert me["user"]["login"] == "641725631"
    _, enrollment = w.enroll("641725631", args["key"])
    assert enrollment["device_status"] == "approved"


@pytest.mark.parametrize("reason", ["attempts", "expired", "configuration"])
def test_setup_rejects_expired_locked_or_changed_configuration(empty_world, reason):
    w = empty_world
    args, verification = setup_start(w)
    expected = "SETUP_CHALLENGE_EXPIRED"
    if reason == "attempts":
        wrong = "00000000" if verification["code"] != "00000000" else "11111111"
        for _ in range(5):
            response = w.call("POST", "/api/setup/complete", body={**verification, "code": wrong}, **args)
            assert response.json()["code"] == "INVALID_EMAIL_CODE"
        with w.owner() as db:
            assert db.get(BootstrapAttempt, uuid.UUID(verification["attempt_id"])).attempts == 5
    elif reason == "expired":
        with w.owner.begin() as db:
            db.execute(update(BootstrapAttempt).values(expires_at=now() - timedelta(seconds=1)))
    else:
        w.ok("PUT", "/api/setup/configuration", body=configuration(1), **args)
        expected = "SETUP_CONFIGURATION_CHANGED"
    assert w.call("POST", "/api/setup/complete", body=verification, **args).json()["code"] == expected
    with w.owner() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_settings_encrypt_secrets_preserve_password_and_use_version_checks(world):
    w = world
    first = w.ok("PUT", "/api/admin/settings", who=w.root, body=configuration(), idem="settings-first-key")
    assert first["version"] == 1 and first["smtp_password_set"] is True
    assert "smtp_password" not in first
    assert SMTP_PASSWORD not in json.dumps(first)
    assert w.ok("GET", "/api/admin/settings", who=w.root) == first
    assert (
        w.ok("PUT", "/api/admin/settings", who=w.root, body=configuration(), idem="settings-first-key")
        == first
    )
    conflict = w.call("PUT", "/api/admin/settings", who=w.root, body=configuration())
    assert conflict.json()["code"] == "CONFIGURATION_VERSION_CONFLICT"
    second = w.ok(
        "PUT", "/api/admin/settings", who=w.root, body=configuration(1, smtp_password=None, smtp_port=587)
    )
    assert second["version"] == 2 and second["smtp_password_set"] is True
    with w.owner() as db:
        encrypted = db.get(SiteConfiguration, "site").encrypted_value
        assert SMTP_PASSWORD not in encrypted and "smtp.example.org" not in encrypted
        assert w.client.app.state.hub.site.read(db)[0]["smtp_password"] == SMTP_PASSWORD
        assert SMTP_PASSWORD not in json.dumps([r.result for r in db.scalars(select(Idempotency))])
        assert SMTP_PASSWORD not in json.dumps([r.details for r in db.scalars(select(Audit))])
    third = w.ok("PUT", "/api/admin/settings", who=w.root, body=configuration(2, smtp_password=""))
    assert third["smtp_password_set"] is False
    delivery = w.ok("POST", "/api/admin/settings/test-email", who=w.root, body={})
    assert delivery["recipient"] == w.root.enrollment.get("email", "root@example.org")


@pytest.mark.parametrize("field,value", [("public_url", "http://example.org"), ("smtp_host", "bad\r\nhost")])
def test_settings_reject_invalid_origin_or_smtp_fields(world, field, value):
    assert (
        world.call(
            "PUT", "/api/admin/settings", who=world.root, body=configuration(**{field: value})
        ).status_code
        == 422
    )


def test_project_admin_cannot_read_or_modify_global_settings_and_other_projects(world):
    w = world
    project, other = w.project(), w.project("project-b")
    admin = w.administrator(project)
    w.member(project)
    w.member(other, login="private-member")
    for path in ["/api/admin/settings", "/api/admin/users", "/api/admin/devices"]:
        assert w.call("GET", path, who=admin).status_code == 403
    assert w.call("PUT", "/api/admin/settings", who=admin, body=configuration()).status_code == 403
    members = w.ok("GET", f"/api/projects/{project}/members", who=admin)["items"]
    assert {u["login"] for u in members} == {"admin-one", "member-one"}
    for suffix in ["members", "member-devices", "grants"]:
        assert w.call("GET", f"/api/projects/{other}/{suffix}", who=admin).status_code == 404


def test_select_approved_project_and_logout_revokes_family(world):
    w = world
    project, other = w.project(), w.project("project-b")
    member, stranger = w.member(project), w.member(other, "stranger")
    gid, other_gid = w.grant(member, project), w.grant(stranger, other)
    assert w.ok("GET", "/api/me/grants", who=member)["items"][0]["project_name"] == "project-a"
    assert (
        w.call("POST", "/api/auth/select-project", who=member, body={"grant_id": other_gid}).status_code
        == 404
    )
    old = member.tokens
    member.tokens = w.ok("POST", "/api/auth/select-project", who=member, body={"grant_id": gid})
    assert member.tokens["grant_id"] == gid
    assert w.call("GET", "/api/me", key=member.key, token=old["access_token"]).status_code == 401
    w.ok("POST", "/api/auth/logout", who=member, body={})
    assert w.call("GET", "/api/me", who=member).status_code == 401
    assert (
        w.call(
            "POST", "/api/auth/refresh", key=member.key, token=member.tokens["refresh_token"], body={}
        ).status_code
        == 401
    )
    assert (
        w.call(
            "POST", "/api/auth/sessions", key=member.key, token=member.enrollment["enrollment_token"], body={}
        ).status_code
        == 401
    )


def test_email_binding_requires_password_and_invalidates_old_sessions_and_grants(world):
    w = world
    project = w.project()
    member = w.member(project)
    other_key, enrollment = w.enroll("member-one")
    other = Identity(member.user_id, other_key, enrollment)
    w.decide(enrollment["approval"])
    w.start(other)
    w.grant(member, project)
    assert (
        w.call(
            "POST",
            "/api/me/email-challenges",
            who=member,
            body={"email": "bound@example.org", "password": "incorrect"},
        ).status_code
        == 401
    )
    challenge = w.ok(
        "POST",
        "/api/me/email-challenges",
        who=member,
        body={"email": "bound@example.org", "password": PASSWORD},
    )
    verification = {"challenge_id": challenge["challenge_id"], "code": w.mail.messages[-1]["code"]}
    # A binding code cannot be used as an ordinary device-verification code.
    invalid = w.call(
        "POST",
        "/api/devices/email-verifications",
        key=member.key,
        token=member.enrollment["enrollment_token"],
        body=verification,
    )
    assert invalid.json()["code"] == "INVALID_EMAIL_CHALLENGE"
    old = member.tokens
    member.tokens = w.ok("POST", "/api/me/email-verifications", who=member, body=verification)
    assert w.ok("GET", "/api/me", who=member)["user"]["email"] == "bound@example.org"
    assert w.ok("GET", "/api/me/grants", who=member)["items"] == []
    assert w.call("GET", "/api/me", key=member.key, token=old["access_token"]).status_code == 401
    assert w.call("GET", "/api/me", who=other).json()["code"] == "EMAIL_VERIFICATION_REQUIRED"
    assert (
        w.call("POST", "/api/auth/refresh", key=member.key, token=old["refresh_token"], body={}).status_code
        == 401
    )
    assert w.call("POST", "/api/me/email-verifications", who=member, body=verification).status_code == 400
    with w.owner() as db:
        assert db.get(Device, uuid.UUID(enrollment["device_id"])).email_verified_at is None
        assert db.get(EmailChallenge, uuid.UUID(challenge["challenge_id"])).consumed_at is not None
