"""Recovery secrets stay hashed; each use revokes old sessions without approving devices."""

import json
from concurrent.futures import ThreadPoolExecutor

from conftest import PASSWORD, DeviceKey
from sqlalchemy import select

from agent_hub.models import Idempotency
from agent_hub.offline_recovery import RecoveryCode


def issue(w, who=None):
    return w.ok("POST", "/api/me/recovery-codes", who=who or w.root, body={"current_password": PASSWORD})


def use(w, code, login="root-admin", **overrides):
    return w.call(
        "POST",
        "/api/auth/offline-recovery",
        key=DeviceKey(),
        body={
            "login": login,
            "recovery_code": code,
            "new_password": "new-password-from-offline-code",
            **overrides,
        },
    )


def test_codes_shown_once_hashed_and_rotation_invalidates_previous_batch(world):
    first = issue(world)
    assert len(set(first["codes"])) == 10 and first["shown_once"]
    status = world.ok("GET", "/api/me/recovery-codes", who=world.root)
    assert status["remaining"] == 10 and "AHRC" not in json.dumps(status)
    with world.owner() as db:
        hashes = list(db.scalars(select(RecoveryCode.digest)))
        assert len(hashes) == 10 and all(len(h) == 64 and h not in first["codes"] for h in hashes)
        cached = json.dumps([r.result for r in db.scalars(select(Idempotency))], default=str)
        assert all(c not in cached for c in first["codes"])
    second = issue(world)
    assert second["previous_codes_revoked"] and second["batch_id"] != first["batch_id"]
    assert use(world, first["codes"][0]).json()["code"] == "RECOVERY_CODE_INVALID"


def test_recovery_revokes_sessions_and_codes_but_does_not_grant_new_device(world):
    w = world
    codes = issue(w)["codes"]
    result = use(w, codes[0])
    assert result.status_code == 200, result.text
    assert result.json()["device_approval_required"] and result.json()["email_verification_required"]
    assert w.call("GET", "/api/me/recovery-codes", who=w.root).status_code == 401
    assert use(w, codes[0]).json()["code"] == "RECOVERY_CODE_INVALID"
    assert use(w, codes[1]).json()["code"] == "RECOVERY_CODE_INVALID"
    enrollment = w.ok(
        "POST",
        "/api/auth/enrollments",
        key=DeviceKey(),
        body={
            "login": "root-admin",
            "password": "new-password-from-offline-code",
            "device_name": "recovered",
            "os": "windows",
        },
    )
    assert enrollment["email_verification_required"] and enrollment["device_status"] != "approved"


def test_concurrent_recovery_consumes_once_and_wrong_password_cannot_issue(world):
    w = world
    assert (
        w.call(
            "POST", "/api/me/recovery-codes", who=w.root, body={"current_password": "incorrect"}
        ).status_code
        == 401
    )
    code = issue(w)["codes"][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: use(w, code), range(2)))
    assert sorted(r.status_code for r in results) == [200, 400]
