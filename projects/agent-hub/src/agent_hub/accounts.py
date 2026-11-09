"""Account lifecycle. OTP possession never approves a member device or grants a project."""

import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Query, Request
from pydantic import EmailStr, Field, SecretStr, field_validator
from sqlalchemy import delete, select, text, update

from . import closeouts
from . import schemas as S
from .db import coordinate
from .mail_outbox import enqueue
from .models import (
    AccountChallenge,
    AccountInvitation,
    AuthSession,
    Device,
    EmailChallenge,
    Enrollment,
    User,
)
from .security import HubError, hash_password, now, rate_limit, verify_password
from .service import require_root


class NewInvitation(S.Input):
    login: S.Login
    email: EmailStr
    duration_days: int = Field(default=3, ge=1, le=7)


class RecoveryStart(S.Input):
    login: S.Login
    email: EmailStr
    purpose: str = Field(pattern=r"^(reset|invitation)$")


class NewPassword(S.Input):
    new_password: SecretStr

    @field_validator("new_password")
    @classmethod
    def valid_password(cls, value):
        return S.NewUser.password_length(value)


class RecoveryComplete(NewPassword):
    challenge_id: uuid.UUID
    code: str = Field(pattern=r"^[0-9]{8}$")


class PasswordChange(NewPassword):
    current_password: SecretStr = Field(max_length=256)


class InvitationAction(S.Input):
    action: str = Field(pattern=r"^(renew|cancel)$")
    duration_days: int = Field(default=3, ge=1, le=7)
    reason: str = Field(min_length=10, max_length=1000)


def invitation_view(entry, user):
    state = "closed" if entry.consumed_at else "expired" if entry.expires_at <= now() else "pending"
    return {
        "invitation_id": str(entry.id),
        "user_id": str(user.id),
        "login": user.login,
        "email": entry.email,
        "project_id": str(entry.project_id) if entry.project_id else None,
        "state": state,
        "expires_at": entry.expires_at.isoformat(),
        "created_at": entry.created_at.isoformat(),
        "can_manage": user.status == "pending_email"
        and not user.email_verified_at
        and user.email == entry.email,
    }


def invitation_action(hub, db, identity, entry, body, request_id):
    user = db.get(User, entry.user_id, populate_existing=True)
    if not user or user.status != "pending_email" or user.email_verified_at or user.email != entry.email:
        raise HubError("INVITATION_NOT_PENDING", 409)
    if body.action == "cancel":
        entry.consumed_at = entry.consumed_at or now()
        db.execute(
            update(AccountChallenge)
            .where(AccountChallenge.invitation_id == entry.id, AccountChallenge.consumed_at.is_(None))
            .values(consumed_at=now())
        )
        result = {"cancelled": True, "invitation_id": str(entry.id)}
    else:
        db.execute(
            update(AccountInvitation)
            .where(
                AccountInvitation.user_id == user.id,
                AccountInvitation.project_id == entry.project_id,
                AccountInvitation.consumed_at.is_(None),
            )
            .values(consumed_at=now())
        )
        clear_admission(db, user.id)
        replacement = AccountInvitation(
            user_id=user.id,
            project_id=entry.project_id,
            created_by=identity.user.id,
            email=entry.email,
            expires_at=now() + timedelta(days=body.duration_days),
        )
        db.add(replacement)
        db.flush()
        result = {**invitation_view(replacement, user), "replaces_id": str(entry.id), "grant_required": True}
    hub.audit(
        db,
        identity.user.id,
        "account.invitation_" + body.action,
        entry.id,
        request_id,
        entry.project_id,
        reason=body.reason,
    )
    return result


def invite(hub, db, identity, body, request_id, project_id=None):
    if project_id:
        hub.manage_members(db, identity, project_id)
        result = hub.invite(
            db,
            identity,
            project_id,
            S.MemberInvitation(
                new_user=S.NewUser(login=body.login, email=body.email, password=secrets.token_urlsafe(48))
            ),
            request_id,
        )
        user = db.get(User, uuid.UUID(result["user"]["id"]))
    else:
        require_root(identity)
        user = hub.create_user(
            db,
            identity,
            S.NewUser(login=body.login, email=body.email, password=secrets.token_urlsafe(48)),
            request_id,
            admin=True,
        )
    entry = AccountInvitation(
        user_id=user.id,
        project_id=project_id,
        created_by=identity.user.id,
        email=user.email,
        expires_at=now() + timedelta(days=body.duration_days),
    )
    db.add(entry)
    db.flush()
    hub.audit(db, identity.user.id, "account.invitation_created", entry.id, request_id, project_id)
    return {
        "invitation_id": str(entry.id),
        "login": user.login,
        "email": user.email,
        "expires_at": entry.expires_at.isoformat(),
        "grant_required": True,
        "instructions": "Open the login page, choose invitation claim, and verify the bound email.",
    }


def start(hub, db, body, thumb, request_id, *, delivery_allowed=True):
    # Missing, disabled and unmatched accounts all return the same shape. No decoy DB rows.
    accepted = {
        "accepted": True,
        "challenge_id": str(uuid.uuid4()),
        "expires_at": (now() + timedelta(seconds=hub.settings.otp_seconds)).isoformat(),
    }
    if not delivery_allowed:
        return accepted
    user = db.scalar(select(User).where(User.login == body.login))
    if not user or user.status not in {"active", "pending_email"} or user.email != str(body.email).lower():
        return accepted
    invitation = None
    if body.purpose == "invitation":
        invitation = db.scalar(
            select(AccountInvitation)
            .where(
                AccountInvitation.user_id == user.id,
                AccountInvitation.email == user.email,
                AccountInvitation.consumed_at.is_(None),
                AccountInvitation.expires_at > now(),
            )
            .order_by(AccountInvitation.created_at.desc())
        )
        if not invitation or user.status != "pending_email":
            return accepted
        if not db.scalar(text("SELECT hub.account_invitation_eligible(:id)"), {"id": invitation.id}):
            return accepted
    elif user.status != "active" or not user.email_verified_at:
        return accepted
    db.execute(
        update(AccountChallenge)
        .where(AccountChallenge.user_id == user.id, AccountChallenge.consumed_at.is_(None))
        .values(consumed_at=now())
    )
    entry = AccountChallenge(
        id=uuid.UUID(accepted["challenge_id"]),
        user_id=user.id,
        invitation_id=invitation.id if invitation else None,
        email=user.email,
        purpose=body.purpose,
        thumbprint=thumb,
        user_epoch=user.auth_epoch,
        expires_at=now() + timedelta(seconds=hub.settings.otp_seconds),
    )
    code = f"{secrets.randbelow(100000000):08d}"
    entry.digest = hub.hash(f"account:{entry.id}:{thumb}:{code}")
    db.add(entry)
    db.flush()
    job = enqueue(
        db,
        hub.settings,
        user.email,
        code=code,
        reference=entry.id,
        reference_kind="account",
        expires_at=entry.expires_at,
    )
    # A durable worker sends later; public recovery responses do not reveal SMTP/account state.
    db.info.get("mail_deliveries", []).remove(job)
    hub.audit(db, user.id, "account.challenge_queued", entry.id, request_id)
    return accepted


def clear_admission(db, user_id):
    devices = select(Device.id).where(Device.user_id == user_id)
    db.execute(delete(Enrollment).where(Enrollment.device_id.in_(devices)))
    db.execute(
        update(EmailChallenge)
        .where(EmailChallenge.device_id.in_(devices), EmailChallenge.consumed_at.is_(None))
        .values(consumed_at=now())
    )
    db.execute(
        update(AccountChallenge)
        .where(AccountChallenge.user_id == user_id, AccountChallenge.consumed_at.is_(None))
        .values(consumed_at=now())
    )


def complete(hub, db, body, thumb, request_id):
    entry = db.get(AccountChallenge, body.challenge_id, populate_existing=True)
    invalid = HubError("ACCOUNT_CHALLENGE_INVALID", 400)
    if (
        not entry
        or entry.thumbprint != thumb
        or entry.consumed_at
        or entry.expires_at <= now()
        or entry.attempts >= 5
    ):
        raise invalid
    user = db.get(User, entry.user_id, populate_existing=True)
    if (
        user.status not in {"active", "pending_email"}
        or user.auth_epoch != entry.user_epoch
        or user.email != entry.email
    ):
        raise invalid
    invitation = db.get(AccountInvitation, entry.invitation_id) if entry.invitation_id else None
    if entry.purpose == "invitation":
        if (
            not invitation
            or invitation.consumed_at
            or invitation.expires_at <= now()
            or user.status != "pending_email"
        ):
            raise invalid
        if not db.scalar(text("SELECT hub.account_invitation_eligible(:id)"), {"id": invitation.id}):
            raise invalid
    elif user.status != "active" or not user.email_verified_at:
        raise invalid
    entry.attempts += 1
    if not hmac.compare_digest(entry.digest, hub.hash(f"account:{entry.id}:{thumb}:{body.code}")):
        raise HubError("ACCOUNT_CHALLENGE_INVALID", 400, persist=True)
    # Freeze recoverable work before invalidating old credentials and grants.
    for key, value in {"id": str(entry.id), "digest": entry.digest, "thumb": thumb}.items():
        db.execute(
            text("SELECT set_config(:key,:value,true)"),
            {"key": "hub.account_recovery_" + key, "value": value},
        )
    jobs = closeouts.create(hub, db, user_id=user.id, reason="credential_recovery", account_owner=True)
    user.password_hash = hash_password(body.new_password.get_secret_value())
    user.auth_epoch += 1
    user.status, user.email_verified_at = "active", now()
    hub.revoke_grants(db, user_id=user.id)
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(state="revoked"))
    clear_admission(db, user.id)
    entry.consumed_at = now()
    if invitation:
        invitation.consumed_at = now()
    hub.audit(db, user.id, "account." + entry.purpose + "_completed", entry.id, request_id)
    return {
        "password_set": True,
        "login_required": True,
        "device_approval_required": True,
        "grant_required": True,
        "old_sessions_revoked": True,
        "closeout_available": bool(jobs),
    }


def change_password(hub, db, identity, body, request_id):
    if not verify_password(identity.user.password_hash, body.current_password.get_secret_value()):
        raise HubError("INVALID_CREDENTIALS", 401)
    identity.user.password_hash = hash_password(body.new_password.get_secret_value())
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == identity.user.id, AuthSession.id != identity.session.id)
        .values(state="revoked")
    )
    clear_admission(db, identity.user.id)
    hub.audit(db, identity.user.id, "account.password_changed", identity.user.id, request_id)
    return {"changed": True, "other_sessions_revoked": True, "current_session_retained": True}


def install(app, hub, actor, proof, mutate, db_dependency, security_sessions):
    DB = Annotated[object, Depends(db_dependency, scope="function")]

    def ip_limit(request, limit):
        rate_limit(
            security_sessions,
            hub.secret,
            f"account-ip:{request.client.host if request.client else 'local'}",
            limit,
            3600,
        )

    @app.post("/api/auth/account-challenges")
    def recovery_start(body: RecoveryStart, request: Request, db: DB):
        ip_limit(request, 30)
        thumb = proof(request)[1]
        uid = db.scalar(select(User.id).where(User.login == body.login))
        delivery_allowed = True
        if uid:
            try:
                rate_limit(security_sessions, hub.secret, f"account-mail:{uid}", 5, 3600)
            except HubError as error:
                if error.code != "RATE_LIMITED":
                    raise
                delivery_allowed = False
        coordinate(db, user_ids=[uid], resources=["account-login:" + body.login])
        db.expire_all()
        return start(hub, db, body, thumb, request.state.request_id, delivery_allowed=delivery_allowed)

    @app.post("/api/auth/account-recovery")
    def recovery_complete(body: RecoveryComplete, request: Request, db: DB):
        ip_limit(request, 120)
        thumb = proof(request)[1]
        uid = db.scalar(select(AccountChallenge.user_id).where(AccountChallenge.id == body.challenge_id))
        coordinate(db, user_ids=[uid], resources=["account-challenge:" + str(body.challenge_id)])
        return complete(hub, db, body, thumb, request.state.request_id)

    @app.post("/api/me/password")
    def password_change(body: PasswordChange, request: Request, db: DB):
        identity = actor(request, db)
        rate_limit(security_sessions, hub.secret, f"password-change:{identity.user.id}", 10, 3600)
        return change_password(hub, db, identity, body, request.state.request_id)

    @app.post("/api/admin/account-invitations")
    def administrator_invitation(body: NewInvitation, request: Request, db: DB):
        identity = actor(request, db, resources=["login:" + body.login, "email:" + str(body.email).lower()])
        require_root(identity)
        return mutate(
            request, db, identity, body, lambda: invite(hub, db, identity, body, request.state.request_id)
        )

    def invitation_list(db, project_id, limit):
        statement = select(AccountInvitation, User).join(User, User.id == AccountInvitation.user_id)
        if project_id:
            statement = statement.where(AccountInvitation.project_id == project_id)
        return {
            "items": [
                invitation_view(entry, user)
                for entry, user in db.execute(
                    statement.order_by(AccountInvitation.created_at.desc(), AccountInvitation.id).limit(limit)
                )
            ]
        }

    @app.get("/api/admin/account-invitations")
    def administrator_invitations(request: Request, db: DB, limit: int = Query(100, ge=1, le=100)):
        require_root(actor(request, db))
        return invitation_list(db, None, limit)

    @app.get("/api/projects/{project_id}/account-invitations")
    def project_invitations(
        project_id: uuid.UUID, request: Request, db: DB, limit: int = Query(100, ge=1, le=100)
    ):
        hub.manage_members(db, actor(request, db), project_id)
        return invitation_list(db, project_id, limit)

    def manage_invitation(invitation_id, body, request, db, project_id=None):
        uid = db.scalar(select(AccountInvitation.user_id).where(AccountInvitation.id == invitation_id))
        identity = actor(request, db, users=[uid], resources=["account-invitation:" + str(invitation_id)])
        if project_id:
            hub.manage_members(db, identity, project_id)
        else:
            require_root(identity)
        entry = db.get(AccountInvitation, invitation_id, populate_existing=True)
        if not entry or project_id and entry.project_id != project_id:
            raise HubError("INVITATION_NOT_ACCESSIBLE", 404)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: invitation_action(hub, db, identity, entry, body, request.state.request_id),
        )

    @app.post("/api/admin/account-invitations/{invitation_id}/actions")
    def administrator_invitation_action(
        invitation_id: uuid.UUID, body: InvitationAction, request: Request, db: DB
    ):
        return manage_invitation(invitation_id, body, request, db)

    @app.post("/api/projects/{project_id}/account-invitations/{invitation_id}/actions")
    def project_invitation_action(
        project_id: uuid.UUID, invitation_id: uuid.UUID, body: InvitationAction, request: Request, db: DB
    ):
        return manage_invitation(invitation_id, body, request, db, project_id)

    @app.post("/api/projects/{project_id}/account-invitations")
    def member_invitation(project_id: uuid.UUID, body: NewInvitation, request: Request, db: DB):
        identity = actor(request, db, resources=["login:" + body.login, "email:" + str(body.email).lower()])
        hub.manage_members(db, identity, project_id)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: invite(hub, db, identity, body, request.state.request_id, project_id),
        )

    @app.post("/api/me/devices/{device_id}/revoke")
    def lost_device(device_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.revoke_device(db, identity, device_id, request.state.request_id, own_only=True),
        )
