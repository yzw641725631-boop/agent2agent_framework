import uuid

from conftest import Identity
from sqlalchemy import update

from agent_hub.models import Approval, AuthSession, Device


def test_root_can_use_password_and_device_email_without_another_root_session(world):
    w = world
    key, enrolled = w.enroll("root-admin")
    assert enrolled["platform_role"] == "superadmin"
    assert enrolled["device_approval_mode"] == "root_email"
    second = Identity(w.root.user_id, key, enrolled)
    pending = w.call("POST", "/api/auth/sessions", key=key, token=enrolled["enrollment_token"], body={})
    assert pending.json()["code"] == "DEVICE_APPROVAL_REQUIRED"
    with w.owner.begin() as db:
        db.execute(update(AuthSession).values(state="revoked"))
    w.email(second)
    w.start(second)
    assert w.ok("GET", "/api/me", who=second)["user"]["platform_role"] == "superadmin"
    with w.owner() as db:
        device = db.get(Device, uuid.UUID(enrolled["device_id"]))
        approval = db.get(Approval, uuid.UUID(enrolled["approval"]["id"]))
        assert device.status == "approved" and str(device.approved_by) == second.user_id
        assert approval.status == "issued" and str(approval.decided_by) == second.user_id
    events = w.ok("GET", "/api/audit", who=second)["items"]
    assert any(event["action"] == "device.superadmin_confirmed_by_email" for event in events)


def test_root_legacy_verified_pending_device_can_continue_after_server_upgrade(world):
    w = world
    key, enrolled = w.enroll("root-admin")
    second = Identity(w.root.user_id, key, enrolled)
    w.email(second)
    with w.owner.begin() as db:
        device = db.get(Device, uuid.UUID(enrolled["device_id"]))
        device.status, device.approved_by = "pending", None
        approval = db.get(Approval, uuid.UUID(enrolled["approval"]["id"]))
        approval.status, approval.decided_by = "pending_approval", None
    result = w.ok("GET", "/api/devices/enrollment", key=key, token=enrolled["enrollment_token"])
    assert result["status"] == "approved"
    w.start(second)


def test_root_self_confirmation_does_not_override_denial_or_revocation(world):
    w = world
    key, enrolled = w.enroll("root-admin")
    second = Identity(w.root.user_id, key, enrolled)
    w.decide(enrolled["approval"], approve=False)
    w.email(second)
    result = w.ok("GET", "/api/devices/enrollment", key=key, token=enrolled["enrollment_token"])
    assert result["status"] == "pending"
    response = w.call("POST", "/api/auth/sessions", key=key, token=enrolled["enrollment_token"], body={})
    assert response.json()["code"] == "DEVICE_APPROVAL_REQUIRED"
    with w.owner.begin() as db:
        db.get(Device, uuid.UUID(enrolled["device_id"])).status = "revoked"
    response = w.call("GET", "/api/devices/enrollment", key=key, token=enrolled["enrollment_token"])
    assert response.json()["code"] == "DEVICE_NOT_ALLOWED"
