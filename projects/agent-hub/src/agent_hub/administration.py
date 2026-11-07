import hmac
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import delete, select, update

from .mail_outbox import enqueue as enqueue_mail
from .models import BootstrapAttempt, Device, EmailChallenge, User
from .security import HubError, hash_password, now, verify_password


def device_view(device, login=None):
    return {
        "id": str(device.id),
        "user_id": str(device.user_id),
        "login": login,
        "name": device.name,
        "os": device.os,
        "status": device.status,
        "thumbprint": device.thumbprint,
        "email_verified": device.email_verified_at is not None,
        "created_at": device.created_at.isoformat(),
    }


def grant_view(grant):
    return {
        "id": str(grant.id),
        "project_id": str(grant.project_id),
        "user_id": str(grant.user_id),
        "device_id": str(grant.device_id),
        "capabilities": grant.capabilities,
        "status": grant.status,
        "expires_at": grant.expires_at.isoformat(),
    }


class Setup:
    def __init__(self, hub):
        self.hub = hub

    @staticmethod
    def required(db):
        return db.scalar(select(User.id).where(User.platform_role == "superadmin")) is None

    def guard(self, db, supplied):
        expected = self.hub.settings.bootstrap_token.get_secret_value()
        if not expected:
            raise HubError("SETUP_DISABLED", 503)
        if not supplied or not hmac.compare_digest(self.hub.hash(expected), self.hub.hash(supplied)):
            raise HubError("INVALID_SETUP_TOKEN", 401)
        if not self.required(db):
            raise HubError("SETUP_ALREADY_COMPLETED", 409)

    def challenge(self, db, body, jwk, thumbprint, request_id):
        config, version = self.hub.site.read(db)
        if not config["smtp_host"] or not config["smtp_from"]:
            raise HubError("MAIL_NOT_CONFIGURED", 503)
        if db.scalar(select(User.id).where(User.login == body.login)):
            raise HubError("USER_ALREADY_EXISTS", 409)
        email = str(body.email).lower()
        if db.scalar(select(User.id).where(User.email == email)):
            raise HubError("USER_ALREADY_EXISTS", 409)
        latest = db.scalar(
            select(BootstrapAttempt)
            .where(BootstrapAttempt.thumbprint == thumbprint)
            .order_by(BootstrapAttempt.created_at.desc())
        )
        if latest and (now() - latest.created_at).total_seconds() < 60:
            raise HubError("RATE_LIMITED", 429)
        db.execute(delete(BootstrapAttempt).where(BootstrapAttempt.thumbprint == thumbprint))
        code = f"{secrets.randbelow(100000000):08d}"
        attempt_id = uuid.uuid4()
        entry = BootstrapAttempt(
            id=attempt_id,
            login=body.login,
            password_hash=hash_password(body.password.get_secret_value()),
            email=email,
            public_jwk=jwk,
            thumbprint=thumbprint,
            device_name=body.device_name,
            os=body.os,
            configuration_version=version,
            challenge_digest=self.hub.hash(f"{attempt_id}:{code}"),
            expires_at=now() + timedelta(seconds=self.hub.settings.otp_seconds),
        )
        db.add(entry)
        db.flush()
        enqueue_mail(
            db,
            self.hub.settings,
            email,
            code=code,
            reference=entry.id,
            reference_kind="bootstrap",
            expires_at=entry.expires_at,
        )
        self.hub.audit(db, None, "setup.email_challenge_queued", attempt_id, request_id)
        return {"attempt_id": str(attempt_id), "expires_at": entry.expires_at.isoformat()}

    def complete(self, db, body, thumbprint, request_id):
        entry = db.get(BootstrapAttempt, body.attempt_id)
        if not entry or entry.thumbprint != thumbprint:
            raise HubError("INVALID_SETUP_CHALLENGE", 400)
        if entry.expires_at <= now() or entry.attempts >= 5:
            raise HubError("SETUP_CHALLENGE_EXPIRED", 400)
        if entry.configuration_version != self.hub.site.read(db)[1]:
            raise HubError("SETUP_CONFIGURATION_CHANGED", 409)
        entry.attempts += 1
        if not hmac.compare_digest(entry.challenge_digest, self.hub.hash(f"{entry.id}:{body.code}")):
            raise HubError("INVALID_EMAIL_CODE", 400, persist=True)
        if db.scalar(select(Device.id).where(Device.thumbprint == thumbprint)):
            raise HubError("DEVICE_NOT_ALLOWED", 409)
        user, device = self.hub.bootstrap_verified(
            db,
            entry.login,
            entry.password_hash,
            entry.email,
            entry.public_jwk,
            thumbprint,
            entry.os,
            entry.device_name,
            request_id,
        )
        db.execute(delete(BootstrapAttempt))
        return self.hub.issue_session(db, user, device)


def email_change_start(hub, db, actor, body, request_id):
    if not verify_password(actor.user.password_hash, body.password.get_secret_value()):
        raise HubError("INVALID_CREDENTIALS", 401)
    email = str(body.email).lower()
    if email == actor.user.email:
        raise HubError("EMAIL_ALREADY_BOUND", 409)
    latest = db.scalar(
        select(EmailChallenge)
        .where(EmailChallenge.device_id == actor.device.id)
        .order_by(EmailChallenge.created_at.desc())
    )
    if latest and (now() - latest.created_at).total_seconds() < 60:
        raise HubError("RATE_LIMITED", 429)
    db.execute(
        update(EmailChallenge)
        .where(EmailChallenge.device_id == actor.device.id, EmailChallenge.consumed_at.is_(None))
        .values(consumed_at=now())
    )
    code = f"{secrets.randbelow(100000000):08d}"
    entry_id = uuid.uuid4()
    entry = EmailChallenge(
        id=entry_id,
        device_id=actor.device.id,
        email=email,
        purpose="bind",
        user_epoch=actor.user.auth_epoch,
        digest=hub.hash(f"{entry_id}:{code}"),
        expires_at=now() + timedelta(seconds=hub.settings.otp_seconds),
    )
    db.add(entry)
    db.flush()
    enqueue_mail(
        db,
        hub.settings,
        email,
        code=code,
        reference=entry.id,
        reference_kind="challenge",
        expires_at=entry.expires_at,
    )
    hub.audit(db, actor.user.id, "email.binding_requested", entry.id, request_id)
    return {"challenge_id": str(entry.id), "expires_at": entry.expires_at.isoformat()}


def email_change_complete(hub, db, actor, body, request_id):
    entry = db.get(EmailChallenge, body.challenge_id)
    if (
        not entry
        or entry.device_id != actor.device.id
        or entry.purpose != "bind"
        or entry.user_epoch != actor.user.auth_epoch
    ):
        raise HubError("INVALID_EMAIL_CHALLENGE", 400)
    if entry.consumed_at or entry.expires_at <= now() or entry.attempts >= 5:
        raise HubError("EMAIL_CHALLENGE_EXPIRED", 400)
    entry.attempts += 1
    if not hmac.compare_digest(entry.digest, hub.hash(f"{entry.id}:{body.code}")):
        raise HubError("INVALID_EMAIL_CODE", 400, persist=True)
    entry.consumed_at = now()
    if db.scalar(select(User.id).where(User.email == entry.email, User.id != actor.user.id)):
        raise HubError("EMAIL_NOT_AVAILABLE", 409, persist=True)
    actor.user.email, actor.user.email_verified_at = entry.email, now()
    actor.user.auth_epoch += 1
    db.execute(update(Device).where(Device.user_id == actor.user.id).values(email_verified_at=None))
    actor.device.email_verified_at = now()
    hub.revoke_grants(db, user_id=actor.user.id)
    hub.audit(db, actor.user.id, "email.binding_changed", actor.user.id, request_id)
    return hub.issue_session(db, actor.user, actor.device)
