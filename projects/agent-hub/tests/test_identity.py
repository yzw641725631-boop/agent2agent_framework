import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from conftest import DeviceKey, Identity
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from agent_hub.api import create_app
from agent_hub.db import context
from agent_hub.models import Approval, EmailChallenge, Grant, Outbox, Project
from agent_hub.security import now


def test_unbound_device_requires_root_approval(world):
    project = world.project()
    member = world.member(project, approve=False)
    response = world.call(
        "POST", "/api/auth/sessions", key=member.key, token=member.enrollment["enrollment_token"], body={}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_APPROVAL_REQUIRED"
    world.decide(member.enrollment["approval"])
    world.start(member)
    assert world.ok("GET", "/api/me", who=member)["user"]["email"] is None
    assert world.ok("GET", "/api/projects", who=member)["items"] == []


def test_new_device_requires_email_and_root_even_for_existing_verified_user(world):
    project = world.project()
    original = world.member(project, email="person@example.org")
    key, enrolled = world.enroll("member-one")
    second = Identity(original.user_id, key, enrolled)
    a = enrolled["approval"]
    denied = world.call(
        "POST",
        f"/api/admin/approvals/{a['id']}/decision",
        who=world.root,
        body={"approve": True, "expected_version": a["version"], "expected_hash": a["payload_hash"]},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "EMAIL_VERIFICATION_REQUIRED"
    world.email(second)
    pending = world.call("POST", "/api/auth/sessions", key=key, token=enrolled["enrollment_token"], body={})
    assert pending.status_code == 401
    world.decide(a)
    world.start(second)
    assert second.enrollment["device_id"] != original.enrollment["device_id"]


def test_admin_creation_activation_and_root_only_project_management(world):
    project = world.project()
    missing = world.call(
        "POST",
        "/api/admin/users/admins",
        who=world.root,
        body={"login": "bad-admin", "password": "long-enough-password"},
    )
    assert missing.status_code == 422
    admin = world.administrator(project)
    for path, body in [
        (
            "/api/admin/users/admins",
            {"login": "other-admin", "password": "long-enough-password", "email": "other@example.org"},
        ),
        ("/api/admin/projects", {"slug": "other-project", "name": "Other"}),
    ]:
        result = world.call("POST", path, who=admin, body=body)
        assert result.status_code == 403
    other_project = world.project("project-b")
    forbidden = world.call(
        "POST",
        f"/api/projects/{other_project}/member-invitations",
        who=admin,
        body={"user_id": admin.user_id},
    )
    assert forbidden.status_code == 404
    member = world.member(project)
    a = world.grant(member, project, requester=admin, approve=False)
    denied = world.call(
        "POST",
        f"/api/admin/approvals/{a['id']}/decision",
        who=admin,
        body={"approve": True, "expected_version": a["version"], "expected_hash": a["payload_hash"]},
    )
    assert denied.status_code == 403
    assert world.ok("GET", "/api/projects", who=member)["items"] == []
    gid = world.decide(a)["grant_id"]
    world.start(member, gid)
    assert world.ok("GET", f"/api/projects/{project}/context", who=member)["id"] == project
    # An ordinary member cannot acquire project-administrator capabilities by requesting them.
    denied = world.call(
        "POST",
        "/api/grant-requests",
        who=member,
        body={
            "project_id": project,
            "user_id": member.user_id,
            "device_id": member.enrollment["device_id"],
            "capabilities": ["project:read", "members:manage"],
        },
    )
    assert denied.status_code == 422


def test_project_isolation_and_no_unapproved_grant(world):
    first, second = world.project(), world.project("project-b")
    member = world.member(first)
    approval = world.grant(member, first, approve=False)
    assert world.call("GET", f"/api/projects/{first}/context", who=member).status_code == 404
    gid = world.decide(approval)["grant_id"]
    world.start(member, gid)
    assert [p["id"] for p in world.ok("GET", "/api/projects", who=member)["items"]] == [first]
    for pid in [second, str(uuid.uuid4())]:
        response = world.call("GET", f"/api/projects/{pid}/context", who=member)
        assert response.status_code == 404
        assert response.json()["code"] == "PROJECT_NOT_ACCESSIBLE"
    assert (
        world.call(
            "GET",
            f"/api/audit?project_id={second}",
            who=member,
            proof=member.key.proof("GET", "/api/audit", member.token),
        ).status_code
        == 404
    )


def test_device_proof_binding_replay_and_url_checks(world):
    stolen = world.call("GET", "/api/me", token=world.root.token, key=DeviceKey())
    assert stolen.status_code == 401
    assert stolen.json()["code"] == "DEVICE_KEY_MISMATCH"
    path = f"/api/projects/{uuid.uuid4()}/context"
    proof = world.root.key.proof("GET", path, world.root.token)
    assert world.call("GET", path, who=world.root, proof=proof).status_code == 404
    reused = world.call("GET", path, who=world.root, proof=proof)
    assert reused.json()["code"] == "DEVICE_PROOF_REPLAY"
    assert (
        world.call(
            "GET", "/api/me", who=world.root, proof=world.root.key.proof("POST", "/api/me", world.root.token)
        ).status_code
        == 401
    )
    assert world.call("GET", "/api/me", token=world.root.token).status_code == 401


def test_refresh_rotation_replay_revokes_family_and_never_expands_scope(world):
    project = world.project()
    member = world.member(project)
    world.start(member, world.grant(member, project))
    old = member.tokens
    refreshed = world.ok("POST", "/api/auth/refresh", key=member.key, token=old["refresh_token"], body={})
    assert refreshed["grant_id"] == old["grant_id"]
    assert refreshed["refresh_expires_at"] == old["refresh_expires_at"]
    assert world.call("GET", "/api/me", key=member.key, token=old["access_token"]).status_code == 401
    assert world.call("GET", "/api/me", key=member.key, token=refreshed["access_token"]).status_code == 200
    replay = world.call("POST", "/api/auth/refresh", key=member.key, token=old["refresh_token"], body={})
    assert replay.json()["code"] == "REFRESH_REPLAY_FAMILY_REVOKED"
    assert world.call("GET", "/api/me", key=member.key, token=refreshed["access_token"]).status_code == 401


def test_member_revocation_immediately_blocks_access_and_renewal(world):
    project = world.project()
    member = world.member(project)
    world.start(member, world.grant(member, project))
    world.ok("POST", f"/api/projects/{project}/members/{member.user_id}/revoke", who=world.root, body={})
    response = world.call("GET", f"/api/projects/{project}/context", who=member)
    assert response.status_code == 403
    assert response.json()["code"] == "PROJECT_ACCESS_REVOKED"
    assert response.json()["normal_operations_allowed"] is False
    assert response.json()["closeout_available"] is True
    assert response.json()["required_action"] == "upload_work_then_uninstall"
    refreshed = world.call(
        "POST", "/api/auth/refresh", key=member.key, token=member.tokens["refresh_token"], body={}
    )
    assert refreshed.json()["code"] == "PROJECT_ACCESS_REVOKED"
    with world.owner() as db:
        assert (
            db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "project.access_revoked"))
            == 1
        )
    world.ok(
        "POST", f"/api/admin/projects/{project}/members/{member.user_id}/restore", who=world.root, body={}
    )
    assert world.call("GET", "/api/me", who=member).json()["code"] == "PROJECT_ACCESS_REVOKED"
    world.start(member, world.grant(member, project))
    assert world.ok("GET", f"/api/projects/{project}/context", who=member)["id"] == project


@pytest.mark.parametrize("control", ["account", "device"])
def test_global_controls_invalidate_existing_sessions(world, control):
    project = world.project()
    member = world.member(project)
    if control == "account":
        path = f"/api/admin/users/{member.user_id}/status"
        world.ok("POST", path, who=world.root, body={"status": "suspended"})
        assert world.call("GET", "/api/me", who=member).status_code == 401
        world.ok("POST", path, who=world.root, body={"status": "active"})
        assert world.call("GET", "/api/me", who=member).json()["code"] == "SESSION_REVOKED"
    else:
        world.ok(
            "POST", f"/api/admin/devices/{member.enrollment['device_id']}/revoke", who=world.root, body={}
        )
        assert world.call("GET", "/api/me", who=member).status_code == 401


def test_approval_payload_tampering_and_changed_hash_refused(world):
    project = world.project()
    member = world.member(project)
    a = world.grant(member, project, approve=False)
    with world.owner.begin() as db:
        row = db.get(Approval, uuid.UUID(a["id"]))
        row.payload = {**row.payload, "capabilities": ["project:read", "members:manage"]}
    response = world.call(
        "POST",
        f"/api/admin/approvals/{a['id']}/decision",
        who=world.root,
        body={"approve": True, "expected_version": a["version"], "expected_hash": a["payload_hash"]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "APPROVAL_PAYLOAD_CHANGED"
    with world.owner() as db:
        assert db.scalar(select(func.count()).select_from(Grant)) == 0


def test_idempotent_project_creation_and_changed_payload(world):
    def create():
        return world.ok(
            "POST",
            "/api/admin/projects",
            who=world.root,
            idem="same-project-key",
            body={"slug": "project-a", "name": "Project A"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results[0] == results[1]
    response = world.call(
        "POST",
        "/api/admin/projects",
        who=world.root,
        idem="same-project-key",
        body={"slug": "project-a", "name": "Changed"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_PAYLOAD_CHANGED"
    # Different initial passwords cannot be mistaken for the same request after SecretStr redaction.
    body = {"login": "admin-two", "email": "two@example.org", "password": "first-password-long"}
    world.ok("POST", "/api/admin/users/admins", who=world.root, idem="same-user-key", body=body)
    response = world.call(
        "POST",
        "/api/admin/users/admins",
        who=world.root,
        idem="same-user-key",
        body={**body, "password": "second-password-long"},
    )
    assert response.status_code == 409


def test_email_failure_budget_single_use_and_send_cooldown(world):
    project = world.project()
    member = world.member(project, approve=False)
    # The optional-email branch is exercised with a separate account.
    other = world.member(project, "member-two", approve=False)
    from agent_hub.models import User

    with world.owner.begin() as db:
        user = db.get(User, uuid.UUID(other.user_id))
        user.email, user.status = "two@example.org", "pending_email"
    kwargs = {"key": other.key, "token": other.enrollment["enrollment_token"]}
    challenge = world.ok("POST", "/api/devices/email-challenges", body={}, **kwargs)
    correct = world.mail.messages[-1]["code"]
    wrong = "00000000" if correct != "00000000" else "11111111"
    assert world.call("POST", "/api/devices/email-challenges", body={}, **kwargs).status_code == 429
    for _ in range(5):
        response = world.call(
            "POST",
            "/api/devices/email-verifications",
            body={"challenge_id": challenge["challenge_id"], "code": wrong},
            **kwargs,
        )
        assert response.json()["code"] == "INVALID_EMAIL_CODE"
    locked = world.call(
        "POST",
        "/api/devices/email-verifications",
        body={"challenge_id": challenge["challenge_id"], "code": correct},
        **kwargs,
    )
    assert locked.json()["code"] == "EMAIL_CHALLENGE_EXPIRED"
    with world.owner() as db:
        assert db.get(EmailChallenge, uuid.UUID(challenge["challenge_id"])).attempts == 5
    bound = world.member(project, "member-three", "three@example.org", approve=False)
    # member() verified this email once; submitting the consumed challenge again cannot pass.
    with world.owner() as db:
        challenge_id = str(
            db.scalar(
                select(EmailChallenge.id).where(
                    EmailChallenge.device_id == uuid.UUID(bound.enrollment["device_id"])
                )
            )
        )
    response = world.call(
        "POST",
        "/api/devices/email-verifications",
        key=bound.key,
        token=bound.enrollment["enrollment_token"],
        body={"challenge_id": challenge_id, "code": world.mail.messages[-1]["code"]},
    )
    assert response.json()["code"] == "EMAIL_CHALLENGE_EXPIRED"
    assert member.enrollment["device_status"] == "pending"


def test_rls_direct_database_access_and_pooled_transaction_reset(world):
    project, other = world.project(), world.project("project-b")
    member = world.member(project)
    world.start(member, world.grant(member, project))
    sessions = world.client.app.state.sessions
    with sessions.begin() as db:
        assert list(db.scalars(select(Project))) == []
        context(db, world.root.user_id, world.root.enrollment["device_id"])
        assert len(list(db.scalars(select(Project)))) == 2
    with sessions.begin() as db:
        assert list(db.scalars(select(Project))) == []
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert [str(p.id) for p in db.scalars(select(Project))] == [project]
        assert db.get(Project, uuid.UUID(other)) is None
    with sessions.begin() as db:
        assert list(db.scalars(select(Project))) == []
        role = db.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one()
        assert tuple(role) == (False, False)


def test_expired_grant_rejects_unexpired_access_and_refresh(world):
    project = world.project()
    member = world.member(project)
    world.start(member, world.grant(member, project))
    with world.owner.begin() as db:
        db.execute(update(Grant).values(expires_at=now() - timedelta(seconds=1)))
    assert world.call("GET", "/api/me", who=member).json()["code"] == "PROJECT_ACCESS_REVOKED"
    assert (
        world.call(
            "POST", "/api/auth/refresh", key=member.key, token=member.tokens["refresh_token"], body={}
        ).json()["code"]
        == "PROJECT_ACCESS_REVOKED"
    )


def test_api_refuses_database_owner(database):
    cfg, owner = database
    bad = cfg.model_copy(
        update={
            "database_url": __import__("pydantic").SecretStr(
                owner.kw["bind"].url.render_as_string(hide_password=False)
            )
        }
    )
    with pytest.raises(RuntimeError, match="non-owner"):
        with TestClient(create_app(bad)):
            pass
