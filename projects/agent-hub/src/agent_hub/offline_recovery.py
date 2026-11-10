"""Offline password recovery is not device approval, email verification or project authorization."""

import base64
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from pydantic import Field, SecretStr
from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select, text, update
from sqlalchemy.orm import Mapped, mapped_column

from . import accounts, closeouts
from . import schemas as S
from .db import coordinate
from .models import AuthSession, Base, Identity, User
from .security import HubError, hash_password, now, rate_limit, verify_password


class RecoveryCode(Identity, Base):
    __tablename__ = "account_recovery_codes"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    batch_id: Mapped[uuid.UUID]
    user_epoch: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Issue(S.Input):
    current_password: SecretStr = Field(max_length=256)


class Recover(accounts.NewPassword):
    login: S.Login
    recovery_code: SecretStr = Field(min_length=30, max_length=100)


def code_digest(hub, login, value):
    clean = value.strip().upper().replace("-", "")
    return hub.hash("offline-recovery:" + login + ":" + clean)


def issue(hub, db, identity, password, request_id):
    if not verify_password(identity.user.password_hash, password):
        raise HubError("INVALID_CREDENTIALS", 401)
    db.execute(
        update(RecoveryCode)
        .where(RecoveryCode.user_id == identity.user.id, RecoveryCode.consumed_at.is_(None))
        .values(consumed_at=now())
    )
    batch, values, expiry = uuid.uuid4(), [], now() + timedelta(days=730)
    for _ in range(10):
        raw = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        value = "AHRC-" + "-".join(raw[i : i + 8] for i in range(0, len(raw), 8))
        values.append(value)
        db.add(
            RecoveryCode(
                user_id=identity.user.id,
                device_id=identity.device.id,
                batch_id=batch,
                user_epoch=identity.user.auth_epoch,
                digest=code_digest(hub, identity.user.login, value),
                expires_at=expiry,
            )
        )
    hub.audit(db, identity.user.id, "account.recovery_codes_rotated", batch, request_id)
    db.flush()
    # Never pass this response through generic persisted idempotency caching.
    return {
        "codes": values,
        "batch_id": str(batch),
        "expires_at": expiry.isoformat(),
        "shown_once": True,
        "previous_codes_revoked": True,
        "device_or_project_approval_bypassed": False,
    }


def recover(hub, db, body, request_id):
    hashed = code_digest(hub, body.login, body.recovery_code.get_secret_value())
    uid = db.scalar(select(func.hub.offline_recovery_user(hashed)))
    coordinate(db, user_ids=[uid], resources=["offline-recovery:" + body.login])
    user = db.get(User, uid, populate_existing=True) if uid else None
    if not user or user.login != body.login or not db.scalar(select(func.hub.offline_recovery_user(hashed))):
        raise HubError("RECOVERY_CODE_INVALID", 400)
    db.execute(text("SELECT set_config('hub.offline_recovery_digest',:digest,true)"), {"digest": hashed})
    jobs = closeouts.create(hub, db, user_id=user.id, reason="credential_recovery", account_owner=True)
    if not db.scalar(select(func.hub.consume_offline_recovery(hashed))):
        raise HubError("RECOVERY_CODE_INVALID", 400)
    user.password_hash, user.auth_epoch = (
        hash_password(body.new_password.get_secret_value()),
        user.auth_epoch + 1,
    )
    hub.revoke_grants(db, user_id=user.id)
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(state="revoked"))
    accounts.clear_admission(db, user.id)
    hub.audit(db, user.id, "account.offline_recovery_completed", user.id, request_id)
    return {
        "password_set": True,
        "login_required": True,
        "old_sessions_revoked": True,
        "all_recovery_codes_invalidated": True,
        "device_approval_required": True,
        "email_verification_required": bool(user.email),
        "grant_required": True,
        "closeout_available": bool(jobs),
    }


def install(app, hub, actor, proof, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]

    @app.get("/api/me/recovery-codes")
    def status(request: Request, db: DB):
        identity = actor(request, db)
        remaining = db.scalar(
            select(func.count(RecoveryCode.id)).where(
                RecoveryCode.user_id == identity.user.id,
                RecoveryCode.user_epoch == identity.user.auth_epoch,
                RecoveryCode.consumed_at.is_(None),
                RecoveryCode.expires_at > now(),
            )
        )
        return {"remaining": remaining, "codes_returned": False}

    @app.post("/api/me/recovery-codes")
    def rotate(body: Issue, request: Request, db: DB):
        identity = actor(request, db)
        rate_limit(security_sessions, hub.secret, f"offline-code-rotate:{identity.user.id}", 5, 3600)
        return issue(hub, db, identity, body.current_password.get_secret_value(), request.state.request_id)

    @app.post("/api/auth/offline-recovery")
    def complete(body: Recover, request: Request, db: DB):
        proof(request)
        rate_limit(security_sessions, hub.secret, f"offline-code-ip:{request.client.host}", 30, 3600)
        rate_limit(security_sessions, hub.secret, f"offline-code-login:{body.login}", 10, 3600)
        return recover(hub, db, body, request.state.request_id)
