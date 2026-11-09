import uuid
from datetime import timedelta

from conftest import PASSWORD, DeviceKey, Identity
from sqlalchemy import select
from test_closeouts import discover, fixture

from agent_hub.models import AccountChallenge, AccountInvitation, Device, Outbox, User
from agent_hub.security import now

NEW_PASSWORD = "changed-account-password-2026"


def challenge(w, key, login, email, purpose="reset"):
    value = w.ok(
        "POST",
        "/api/auth/account-challenges",
        key=key,
        body={"login": login, "email": email, "purpose": purpose},
    )
    # Deliver the actual encrypted outbox intent without waiting for its recovery lease.
    with w.owner() as db:
        job = db.scalar(
            select(Outbox.id).where(
                Outbox.kind == "mail.delivery", Outbox.payload["reference"].astext == value["challenge_id"]
            )
        )
    if job:
        w.client.app.state.deliveries.deliver(job)
    return value


def complete(w, key, value, code=None):
    return w.call(
        "POST",
        "/api/auth/account-recovery",
        key=key,
        body={
            "challenge_id": value["challenge_id"],
            "code": code or w.mail.messages[-1]["code"],
            "new_password": NEW_PASSWORD,
        },
    )


def test_email_invitation_claim_sets_password_without_approving_device_or_issuing_grants(world):
    w, key = world, DeviceKey()
    project = w.project()
    invitation = w.ok(
        "POST",
        f"/api/projects/{project}/account-invitations",
        who=w.root,
        body={"login": "invited-member", "email": "invite@example.org"},
    )
    value = challenge(w, key, "invited-member", invitation["email"], "invitation")
    result = complete(w, key, value)
    assert result.status_code == 200, result.text
    assert result.json()["login_required"] and result.json()["grant_required"]
    assert "access_token" not in result.json()
    enrollment = w.ok(
        "POST",
        "/api/auth/enrollments",
        key=key,
        body={
            "login": "invited-member",
            "password": NEW_PASSWORD,
            "device_name": "claiming-device",
            "os": "windows",
        },
    )
    assert enrollment["device_status"] == "pending" and enrollment["email_verification_required"]
    assert (
        w.call(
            "POST", "/api/auth/sessions", key=key, token=enrollment["enrollment_token"], body={}
        ).status_code
        == 401
    )
    assert complete(w, key, value).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    with w.owner() as db:
        assert db.get(AccountInvitation, uuid.UUID(invitation["invitation_id"])).consumed_at


def test_recovery_code_is_device_bound_single_use_and_limited_to_five_attempts(world):
    w, key, other = world, DeviceKey(), DeviceKey()
    value = challenge(w, key, "root-admin", "root@example.org")
    code = w.mail.messages[-1]["code"]
    assert complete(w, other, value, code).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        assert complete(w, key, value, wrong).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    assert complete(w, key, value, code).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    with w.owner() as db:
        assert db.get(AccountChallenge, uuid.UUID(value["challenge_id"])).attempts == 5
    assert w.ok("GET", "/api/me", who=w.root)["user"]["login"] == "root-admin"


def test_unknown_unbound_mismatched_and_disabled_accounts_return_same_shape_without_mail(world):
    w, key = world, DeviceKey()
    project = w.project()
    w.member(project, "no-email")
    for login, email in [
        ("missing-account", "none@example.org"),
        ("no-email", "none@example.org"),
        ("root-admin", "wrong@example.org"),
    ]:
        before = len(w.mail.messages)
        value = challenge(w, key, login, email)
        assert set(value) == {"accepted", "challenge_id", "expires_at"} and value["accepted"]
        assert len(w.mail.messages) == before
        assert complete(w, key, value, "12345678").json()["code"] == "ACCOUNT_CHALLENGE_INVALID"


def test_recovery_preserves_member_work_and_releases_lease_before_revoking_sessions(world):
    w = world
    project, _, task, _, member, body, archive = fixture(w)
    with w.owner.begin() as db:
        user = db.get(User, uuid.UUID(member.user_id))
        user.email, user.email_verified_at = "recover-member@example.org", now()
        db.get(Device, uuid.UUID(member.enrollment["device_id"])).email_verified_at = now()
        login = user.login
    key = DeviceKey()
    value = challenge(w, key, login, "recover-member@example.org")
    result = complete(w, key, value)
    assert result.status_code == 200, result.text
    assert result.json()["closeout_available"]
    assert w.call("GET", "/api/me", who=member).status_code == 401
    job = discover(w, member)[0]
    assert job["reason"] == "credential_recovery" and not job["normal_operations_allowed"]
    from test_closeouts import put

    put(w, member, job, body, archive)
    current = w.ok("GET", f"/api/projects/{project}/tasks/{task['id']}", who=w.root)["task"]
    assert current["status"] == "ready" and current["active_run_id"] is None


def test_password_change_keeps_current_session_and_invalidates_other_sessions_and_old_password(world):
    w = world
    key, enrollment = w.enroll("root-admin")
    second = Identity(w.root.user_id, key, enrollment)
    w.email(second)
    w.start(second)
    w.ok(
        "POST",
        "/api/me/password",
        who=w.root,
        body={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert w.ok("GET", "/api/me", who=w.root)["user"]["login"] == "root-admin"
    assert w.call("GET", "/api/me", who=second).status_code == 401
    assert (
        w.call(
            "POST",
            "/api/auth/enrollments",
            key=DeviceKey(),
            body={"login": "root-admin", "password": PASSWORD, "device_name": "test", "os": "macos"},
        ).json()["code"]
        == "INVALID_CREDENTIALS"
    )
    assert (
        w.call(
            "POST", "/api/auth/sessions", key=second.key, token=enrollment["enrollment_token"], body={}
        ).status_code
        == 401
    )


def test_member_can_revoke_own_lost_device_from_platform_session_with_frozen_handoff(world):
    w = world
    _, _, _, _, member, body, archive = fixture(w)
    with w.owner() as db:
        login = db.get(User, uuid.UUID(member.user_id)).login
    key, enrollment = w.enroll(login)
    current = Identity(member.user_id, key, enrollment)
    w.decide(enrollment["approval"])
    w.start(current)
    path = f"/api/me/devices/{member.enrollment['device_id']}/revoke"
    result = w.ok("POST", path, who=current, body={})
    assert result["closeout_available"] and not result["normal_operations_allowed"]
    from test_closeouts import put

    put(w, member, discover(w, member)[0], body, archive)
    assert w.call("GET", "/api/me", who=member).status_code == 401
    assert w.ok("GET", "/api/me", who=current)["user"]["id"] == member.user_id
    assert (
        w.call(
            "POST", f"/api/me/devices/{w.root.enrollment['device_id']}/revoke", who=current, body={}
        ).status_code
        == 409
    )


def test_revoked_invitation_and_suspended_user_cannot_use_previously_sent_code(world):
    w, key = world, DeviceKey()
    project = w.project()
    w.ok(
        "POST",
        f"/api/projects/{project}/account-invitations",
        who=w.root,
        body={"login": "removed-invite", "email": "removed@example.org"},
    )
    value = challenge(w, key, "removed-invite", "removed@example.org", "invitation")
    with w.owner() as db:
        user_id = str(db.scalar(select(User.id).where(User.login == "removed-invite")))
    w.ok("POST", f"/api/projects/{project}/members/{user_id}/revoke", who=w.root, body={})
    assert complete(w, key, value).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    assert (
        w.call(
            "POST",
            "/api/admin/account-invitations",
            who=w.member(project, "ordinary-inviter"),
            body={"login": "bad-admin", "email": "bad@example.org"},
        ).status_code
        == 403
    )


def test_invitation_renewal_replaces_expired_invite_and_invalidates_old_challenge(world):
    w, key = world, DeviceKey()
    project = w.project()
    base = f"/api/projects/{project}/account-invitations"
    first = w.ok("POST", base, who=w.root, body={"login": "renew-invite", "email": "renew@example.org"})
    old = challenge(w, key, first["login"], first["email"], "invitation")
    code = w.mail.messages[-1]["code"]
    with w.owner.begin() as db:
        db.get(AccountInvitation, uuid.UUID(first["invitation_id"])).expires_at = now() - timedelta(seconds=1)
    renewed = w.ok(
        "POST",
        f"{base}/{first['invitation_id']}/actions",
        who=w.root,
        body={"action": "renew", "reason": "The previous invitation expired before the user joined."},
    )
    assert renewed["invitation_id"] != first["invitation_id"] and renewed["grant_required"]
    assert complete(w, key, old, code).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    current = challenge(w, key, first["login"], first["email"], "invitation")
    assert complete(w, key, current).status_code == 200
    assert (
        w.call(
            "POST",
            f"{base}/{renewed['invitation_id']}/actions",
            who=w.root,
            body={"action": "renew", "reason": "Already claimed accounts must not be reset by invitations."},
        ).status_code
        == 409
    )


def test_cancelled_invitation_cannot_be_claimed_or_managed_from_another_project(world):
    w, key = world, DeviceKey()
    project, other = w.project(), w.project("other-invitations")
    admin = w.administrator(other)
    base = f"/api/projects/{project}/account-invitations"
    invited = w.ok("POST", base, who=w.root, body={"login": "cancel-invite", "email": "cancel@example.org"})
    value = challenge(w, key, invited["login"], invited["email"], "invitation")
    code = w.mail.messages[-1]["code"]
    action = {"action": "cancel", "reason": "The invitation is no longer required by the project."}
    assert (
        w.call(
            "POST",
            f"/api/projects/{other}/account-invitations/{invited['invitation_id']}/actions",
            who=admin,
            body=action,
        ).status_code
        == 404
    )
    assert w.ok("GET", f"/api/projects/{other}/account-invitations", who=admin)["items"] == []
    w.ok("POST", f"{base}/{invited['invitation_id']}/actions", who=w.root, body=action)
    assert complete(w, key, value, code).json()["code"] == "ACCOUNT_CHALLENGE_INVALID"
    listed = w.ok("GET", base, who=w.root)["items"]
    assert listed[0]["state"] == "closed"
