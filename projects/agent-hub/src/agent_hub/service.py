import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

from . import schemas as S
from .db import context, coordinate
from .mail_outbox import enqueue as enqueue_mail
from .models import (
    Approval,
    Audit,
    AuthSession,
    Device,
    EmailChallenge,
    Enrollment,
    Grant,
    Idempotency,
    Membership,
    Outbox,
    Project,
    User,
)
from .security import HubError, digest, hash_password, now, payload_hash, verify_password
from .site import SiteSettings

MEMBER_CAPS = {"project:read", "documents:read", "documents:propose", "tasks:claim", "tasks:write",
               "tasks:review", "tasks:test", "logs:read"}
ADMIN_CAPS = MEMBER_CAPS | {
    "tasks:publish",
    "members:manage",
    "grants:request",
    "audit:read",
    "documents:manage",
}


@dataclass
class Principal:
    user: User
    device: Device
    session: AuthSession
    grant: Grant | None


def require_root(actor: Principal):
    if actor.user.platform_role != "superadmin":
        raise HubError("SUPERADMIN_REQUIRED")


def approval_view(a: Approval):
    return {
        "id": str(a.id),
        "kind": a.kind,
        "payload": a.payload,
        "payload_hash": a.payload_hash,
        "version": a.version,
        "status": a.status,
        "subject_id": str(a.subject_id),
        "requester_id": str(a.requester_id),
        "expires_at": a.expires_at.isoformat(),
    }


def user_view(u: User):
    return {
        "id": str(u.id),
        "login": u.login,
        "status": u.status,
        "platform_role": u.platform_role,
        "admin_eligible": u.admin_eligible,
        "email": u.email,
        "email_verified": u.email_verified_at is not None,
    }


class Hub:
    def __init__(self, settings, mailer):
        self.settings, self.mailer = settings, mailer
        self.secret = settings.secret.get_secret_value()
        self.site = SiteSettings(settings)

    def hash(self, value: str):
        return digest(self.secret, value)

    def lock_session(
        self,
        db,
        token,
        thumbprint,
        *,
        refresh=False,
        user_ids=(),
        project_ids=(),
        device_id=None,
        approval_id=None,
        grant_id=None,
        resources=(),
        exclusive=True,
    ):
        column = AuthSession.refresh_digest if refresh else AuthSession.access_digest
        subject = db.execute(
            select(AuthSession.user_id, Grant.project_id)
            .join(Device, Device.id == AuthSession.device_id)
            .outerjoin(Grant, Grant.id == AuthSession.grant_id)
            .where(column == self.hash(token), Device.thumbprint == thumbprint)
        ).first()
        if subject is None:
            return
        users, projects = {subject[0], *user_ids}, {subject[1], *project_ids}
        if device_id:
            users.add(db.scalar(select(Device.user_id).where(Device.id == device_id)))
        if approval_id:
            approval = db.execute(
                select(Approval.subject_id, Approval.payload).where(Approval.id == approval_id)
            ).first()
            if approval:
                users.add(approval[0])
                try:
                    projects.add(uuid.UUID(approval[1]["project_id"]))
                except (KeyError, TypeError, ValueError):
                    pass
        if grant_id:
            projects.add(db.scalar(select(Grant.project_id).where(Grant.id == grant_id)))
        coordinate(db, user_ids=users, project_ids=projects, resources=resources, exclusive=exclusive)
        # All authorization reads must see state committed before the locks were acquired.
        db.expire_all()

    def lock_enrollment(self, db, token, thumbprint, *, grant_id=None):
        user_id = db.scalar(
            select(Device.user_id)
            .join(Enrollment, Enrollment.device_id == Device.id)
            .where(Enrollment.digest == self.hash(token), Device.thumbprint == thumbprint)
        )
        if user_id:
            project_id = db.scalar(select(Grant.project_id).where(Grant.id == grant_id)) if grant_id else None
            coordinate(db, user_ids=[user_id], project_ids=[project_id])
            db.expire_all()

    def audit(self, db, actor_id, action, object_id, request_id, project_id=None, **details):
        db.add(
            Audit(
                actor_id=actor_id,
                project_id=project_id,
                action=action,
                object_id=str(object_id),
                request_id=request_id,
                details=details,
            )
        )

    def approval(self, db, kind, requester_id, subject_id, payload):
        a = Approval(
            kind=kind,
            requester_id=requester_id,
            subject_id=subject_id,
            payload=payload,
            payload_hash=payload_hash(payload),
            expires_at=now() + timedelta(days=7),
        )
        db.add(a)
        db.flush()
        return a

    def bootstrap(self, db, login, password, email, jwk, thumbprint, os, request_id="local-bootstrap"):
        return self.bootstrap_verified(
            db, login, hash_password(password), email, jwk, thumbprint, os, "bootstrap-device", request_id
        )

    def bootstrap_verified(
        self, db, login, password_hash, email, jwk, thumbprint, os, device_name, request_id
    ):
        if db.scalar(select(User.id).where(User.platform_role == "superadmin")):
            raise HubError("SUPERADMIN_ALREADY_EXISTS", 409)
        u = User(
            login=login,
            password_hash=password_hash,
            email=email.lower(),
            email_verified_at=now(),
            platform_role="superadmin",
            admin_eligible=True,
            status="active",
        )
        db.add(u)
        db.flush()
        d = Device(
            user_id=u.id,
            public_jwk=jwk,
            thumbprint=thumbprint,
            name=device_name,
            os=os,
            status="approved",
            email_verified_at=now(),
            approved_by=u.id,
        )
        db.add(d)
        db.flush()
        self.audit(db, u.id, "superadmin.bootstrap", d.id, request_id)
        return u, d

    def enroll(self, db, body: S.Enroll, jwk, thumbprint, request_id):
        user = db.scalar(select(User).where(User.login == body.login))
        if not verify_password(user.password_hash if user else None, body.password.get_secret_value()):
            raise HubError("INVALID_CREDENTIALS", 401)
        if user.status not in {"active", "pending_email"}:
            raise HubError("ACCOUNT_INACTIVE", 401)
        device = db.scalar(select(Device).where(Device.thumbprint == thumbprint))
        if device and (device.user_id != user.id or device.status == "revoked"):
            raise HubError("DEVICE_NOT_ALLOWED", 401)
        if device is None:
            device = Device(
                user_id=user.id, public_jwk=jwk, thumbprint=thumbprint, name=body.device_name, os=body.os
            )
            db.add(device)
            db.flush()
            self.approval(
                db, "device", user.id, user.id, {"device_id": str(device.id), "thumbprint": thumbprint}
            )
            self.audit(db, user.id, "device.enrolled", device.id, request_id)
        token = secrets.token_urlsafe(32)
        db.add(
            Enrollment(
                device_id=device.id,
                digest=self.hash(token),
                expires_at=now() + timedelta(seconds=self.settings.enrollment_seconds),
            )
        )
        approval = db.scalar(
            select(Approval)
            .where(
                Approval.kind == "device",
                Approval.subject_id == user.id,
                Approval.payload["device_id"].astext == str(device.id),
            )
            .order_by(Approval.created_at.desc())
        )
        if device.status == "pending" and approval and approval.status == "denied":
            raise HubError("DEVICE_APPLICATION_DENIED", 403)
        if device.status == "pending" and (not approval or approval.expires_at <= now()):
            if approval and approval.status == "pending_approval":
                approval.status = "expired"
            approval = self.approval(
                db, "device", user.id, user.id, {"device_id": str(device.id), "thumbprint": thumbprint}
            )
            self.audit(db, user.id, "device.approval_renewed", device.id, request_id)
        self.confirm_superadmin_device(db, user, device, request_id)
        return {
            "enrollment_token": token,
            "device_id": str(device.id),
            "device_status": device.status,
            "approval": approval_view(approval) if approval else None,
            "email_verification_required": bool(user.email and not device.email_verified_at),
            "platform_role": user.platform_role,
            "device_approval_mode": "root_email" if user.platform_role == "superadmin" else "superadmin",
        }

    def confirm_superadmin_device(self, db, user, device, request_id):
        """Confirm a root's pending key after password enrollment and device email OTP.

        Callers hold the user lock and require possession of this device key.
        Explicit device revocation and application denial cannot be overridden.
        """
        if (
            user.platform_role != "superadmin"
            or user.status != "active"
            or not user.email
            or not user.email_verified_at
            or device.user_id != user.id
            or device.status != "pending"
            or not device.email_verified_at
        ):
            return False
        approval = db.scalar(
            select(Approval)
            .where(
                Approval.kind == "device",
                Approval.subject_id == user.id,
                Approval.payload["device_id"].astext == str(device.id),
            )
            .order_by(Approval.created_at.desc(), Approval.id.desc())
        )
        if (
            not approval
            or approval.status != "pending_approval"
            or approval.expires_at <= now()
            or approval.payload.get("thumbprint") != device.thumbprint
            or approval.payload_hash != payload_hash(approval.payload)
        ):
            return False
        device.status, device.approved_by = "approved", user.id
        approval.status, approval.decided_by = "issued", user.id
        self.audit(
            db,
            user.id,
            "device.superadmin_confirmed_by_email",
            device.id,
            request_id,
            approval_id=str(approval.id),
        )
        return True

    def enrollment_identity(self, db, token, thumbprint):
        entry = db.scalar(select(Enrollment).where(Enrollment.digest == self.hash(token)))
        if not entry or entry.expires_at <= now():
            raise HubError("ENROLLMENT_EXPIRED", 401)
        device = db.get(Device, entry.device_id)
        user = db.get(User, device.user_id)
        if device.thumbprint != thumbprint or device.status == "revoked":
            raise HubError("DEVICE_NOT_ALLOWED", 401)
        if user.status not in {"active", "pending_email"}:
            raise HubError("ACCOUNT_INACTIVE", 401)
        return user, device

    def send_email(self, db, user, device, request_id):
        if not user.email:
            raise HubError("NO_EMAIL_BOUND", 409)
        latest = db.scalar(
            select(EmailChallenge)
            .where(EmailChallenge.device_id == device.id)
            .order_by(EmailChallenge.created_at.desc())
        )
        if latest and (now() - latest.created_at).total_seconds() < 60:
            raise HubError("RATE_LIMITED", 429)
        db.execute(
            update(EmailChallenge)
            .where(EmailChallenge.device_id == device.id, EmailChallenge.consumed_at.is_(None))
            .values(consumed_at=now())
        )
        code = f"{secrets.randbelow(100000000):08d}"
        entry = EmailChallenge(
            id=uuid.uuid4(),
            device_id=device.id,
            email=user.email,
            expires_at=now() + timedelta(seconds=self.settings.otp_seconds),
        )
        entry.digest = self.hash(f"{entry.id}:{code}")
        db.add(entry)
        db.flush()
        enqueue_mail(
            db,
            self.settings,
            user.email,
            code=code,
            reference=entry.id,
            reference_kind="challenge",
            expires_at=entry.expires_at,
        )
        self.audit(db, user.id, "email.challenge_queued", device.id, request_id)
        return {"challenge_id": str(entry.id), "expires_at": entry.expires_at.isoformat()}

    def verify_email(self, db, user, device, body: S.VerifyEmail, request_id):
        entry = db.get(EmailChallenge, body.challenge_id)
        if (
            not entry
            or entry.device_id != device.id
            or entry.email != user.email
            or entry.purpose != "device"
        ):
            raise HubError("INVALID_EMAIL_CHALLENGE", 400)
        if entry.consumed_at or entry.expires_at <= now() or entry.attempts >= 5:
            raise HubError("EMAIL_CHALLENGE_EXPIRED", 400)
        entry.attempts += 1
        if not hmac.compare_digest(entry.digest, self.hash(f"{entry.id}:{body.code}")):
            raise HubError("INVALID_EMAIL_CODE", 400, persist=True)
        entry.consumed_at = now()
        user.email_verified_at = now()
        device.email_verified_at = now()
        if user.status == "pending_email":
            user.status = "active"
        self.confirm_superadmin_device(db, user, device, request_id)
        self.audit(db, user.id, "email.verified", device.id, request_id)
        return {
            "verified": True,
            "device_approval_required": device.status != "approved",
            "device_status": device.status,
        }

    def validate_identity(self, user, device, session=None):
        if user.status != "active" or (user.admin_eligible and not user.email_verified_at):
            raise HubError("ACCOUNT_INACTIVE", 401)
        if device.status != "approved":
            raise HubError("DEVICE_APPROVAL_REQUIRED", 401)
        if user.email and not device.email_verified_at:
            raise HubError("EMAIL_VERIFICATION_REQUIRED", 401)
        if session and (session.user_epoch != user.auth_epoch or session.device_epoch != device.epoch):
            raise HubError("SESSION_REVOKED", 401)

    def validate_grant(self, db, user, device, grant):
        member = (
            db.scalar(
                select(Membership).where(
                    Membership.project_id == grant.project_id, Membership.user_id == user.id
                )
            )
            if grant
            else None
        )
        # Project SELECT is protected by RLS; establish context before looking it up.
        context(db, user.id, device.id, grant.id if grant else None)
        project = db.get(Project, grant.project_id) if grant else None
        if (
            not grant
            or not member
            or member.status != "active"
            or not project
            or project.status != "active"
            or grant.user_id != user.id
            or grant.device_id != device.id
            or grant.status != "active"
            or grant.expires_at <= now()
            or grant.membership_epoch != member.epoch
            or grant.user_epoch != user.auth_epoch
            or grant.device_epoch != device.epoch
        ):
            raise HubError(
                "PROJECT_ACCESS_REVOKED",
                project_id=str(grant.project_id) if grant else None,
                normal_operations_allowed=False,
                required_action="stop_normal_operations",
                closeout_available=False,
            )
        return grant

    def issue_session(self, db, user, device, grant=None, *, family=None, family_expiry=None):
        self.confirm_superadmin_device(db, user, device, "session-device-confirmation")
        self.validate_identity(user, device)
        if grant:
            self.validate_grant(db, user, device, grant)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        expiry = family_expiry or now() + timedelta(seconds=self.settings.refresh_seconds)
        s = AuthSession(
            user_id=user.id,
            device_id=device.id,
            grant_id=grant.id if grant else None,
            family_id=family or uuid.uuid4(),
            access_digest=self.hash(access),
            refresh_digest=self.hash(refresh),
            user_epoch=user.auth_epoch,
            device_epoch=device.epoch,
            access_expires_at=min(
                now() + timedelta(seconds=self.settings.access_seconds),
                expiry,
                grant.expires_at if grant else expiry,
            ),
            refresh_expires_at=min(expiry, grant.expires_at if grant else expiry),
        )
        db.add(s)
        db.flush()
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "DPoP",
            "session_id": str(s.id),
            "grant_id": str(s.grant_id) if s.grant_id else None,
            "expires_at": s.access_expires_at.isoformat(),
            "refresh_expires_at": s.refresh_expires_at.isoformat(),
        }

    def authenticate(self, db, token, thumbprint, *, refresh=False):
        column = AuthSession.refresh_digest if refresh else AuthSession.access_digest
        s = db.scalar(select(AuthSession).where(column == self.hash(token)))
        if not s:
            raise HubError("INVALID_SESSION", 401)
        user, device = db.get(User, s.user_id), db.get(Device, s.device_id)
        if device.thumbprint != thumbprint:
            raise HubError("DEVICE_KEY_MISMATCH", 401)
        if refresh and s.state == "rotated":
            db.execute(
                update(AuthSession).where(AuthSession.family_id == s.family_id).values(state="revoked")
            )
            self.audit(db, user.id, "session.refresh_replay", s.family_id, "refresh-replay")
            raise HubError("REFRESH_REPLAY_FAMILY_REVOKED", 401, persist=True)
        self.validate_identity(user, device, s)
        grant = db.get(Grant, s.grant_id) if s.grant_id else None
        if s.grant_id:
            self.validate_grant(db, user, device, grant)
        if s.state != "active" or (s.refresh_expires_at if refresh else s.access_expires_at) <= now():
            raise HubError("SESSION_EXPIRED_OR_REVOKED", 401)
        context(db, user.id, device.id, s.grant_id)
        return Principal(user, device, s, grant)

    def project(self, db, actor, project_id, capability="project:read"):
        if actor.user.platform_role != "superadmin":
            if (
                not actor.grant
                or actor.grant.project_id != project_id
                or capability not in actor.grant.capabilities
            ):
                raise HubError("PROJECT_NOT_ACCESSIBLE", 404)
            self.validate_grant(db, actor.user, actor.device, actor.grant)
        project = db.get(Project, project_id)
        if not project or project.status != "active":
            raise HubError("PROJECT_NOT_ACCESSIBLE", 404)
        return project

    def manage_members(self, db, actor, project_id):
        self.project(db, actor, project_id, "members:manage")
        if actor.user.platform_role != "superadmin":
            member = db.scalar(
                select(Membership).where(
                    Membership.project_id == project_id, Membership.user_id == actor.user.id
                )
            )
            if not member or member.role != "administrator" or member.status != "active":
                raise HubError("PROJECT_NOT_ACCESSIBLE", 404)

    def create_user(self, db, actor, body: S.NewUser, request_id, admin=False):
        if admin:
            require_root(actor)
            if not body.email:
                raise HubError("ADMIN_EMAIL_REQUIRED", 422)
        email = str(body.email).lower() if body.email else None
        if db.scalar(select(User.id).where(User.login == body.login)) or (
            email and db.scalar(select(User.id).where(User.email == email))
        ):
            raise HubError("USER_ALREADY_EXISTS", 409)
        user = User(
            login=body.login,
            password_hash=hash_password(body.password.get_secret_value()),
            email=email,
            admin_eligible=admin,
            status="pending_email" if email else "active",
        )
        db.add(user)
        db.flush()
        self.audit(db, actor.user.id, "user.created", user.id, request_id, admin_eligible=admin)
        return user

    def create_project(self, db, actor, body: S.NewProject, request_id):
        require_root(actor)
        if db.scalar(select(Project.id).where(Project.slug == body.slug)):
            raise HubError("PROJECT_ALREADY_EXISTS", 409)
        p = Project(slug=body.slug, name=body.name, created_by=actor.user.id)
        db.add(p)
        db.flush()
        self.audit(db, actor.user.id, "project.created", p.id, request_id, p.id)
        return {"id": str(p.id), "slug": p.slug, "name": p.name}

    def assign_admin(self, db, actor, project_id, user_id, request_id):
        require_root(actor)
        self.project(db, actor, project_id)
        user = db.get(User, user_id)
        if not user or not user.admin_eligible or user.status != "active" or not user.email_verified_at:
            raise HubError("VERIFIED_ADMIN_REQUIRED", 409)
        member = self.membership(db, project_id, user_id)
        if not member:
            member = Membership(project_id=project_id, user_id=user_id, role="administrator")
            db.add(member)
        else:
            member.role, member.status, member.epoch = "administrator", "active", member.epoch + 1
            self.revoke_grants(db, project_id=project_id, user_id=user_id)
        db.flush()
        self.audit(db, actor.user.id, "project.administrator_assigned", user_id, request_id, project_id)
        return {"user_id": str(user_id), "role": member.role, "grant_required": True}

    @staticmethod
    def membership(db, project_id, user_id):
        return db.scalar(
            select(Membership).where(Membership.project_id == project_id, Membership.user_id == user_id)
        )

    def invite(self, db, actor, project_id, body: S.MemberInvitation, request_id):
        self.manage_members(db, actor, project_id)
        if bool(body.user_id) == bool(body.new_user):
            raise HubError("PROVIDE_USER_ID_OR_NEW_USER", 422)
        user = (
            db.get(User, body.user_id)
            if body.user_id
            else self.create_user(db, actor, body.new_user, request_id)
        )
        if not user or user.platform_role == "superadmin":
            raise HubError("USER_NOT_ACCESSIBLE", 404)
        member = self.membership(db, project_id, user.id)
        if member:
            # Reinstatement is a new root-controlled authorization, never an admin's silent reactivation.
            raise HubError("MEMBERSHIP_ALREADY_EXISTS", 409)
        db.add(Membership(project_id=project_id, user_id=user.id))
        db.flush()
        self.audit(db, actor.user.id, "project.member_invited", user.id, request_id, project_id)
        return {"user": user_view(user), "grant_required": True}

    def request_grant(self, db, actor, body: S.GrantRequest, request_id):
        if actor.user.id != body.user_id:
            self.manage_members(db, actor, body.project_id)
        else:
            # A platform session may request its first grant using its known project ID.
            if actor.user.platform_role != "superadmin" and not self.membership(
                db, body.project_id, body.user_id
            ):
                raise HubError("PROJECT_NOT_ACCESSIBLE", 404)
        user, device = db.get(User, body.user_id), db.get(Device, body.device_id)
        member = self.membership(db, body.project_id, body.user_id)
        if not user or not device or device.user_id != user.id or not member or member.status != "active":
            raise HubError("SUBJECT_NOT_ELIGIBLE", 409)
        self.validate_identity(user, device)
        caps = set(body.capabilities)
        ceiling = ADMIN_CAPS if member.role == "administrator" else MEMBER_CAPS
        if not caps <= ceiling or "project:read" not in caps:
            raise HubError("CAPABILITY_NOT_ALLOWED", 422)
        payload = {
            "project_id": str(body.project_id),
            "user_id": str(user.id),
            "device_id": str(device.id),
            "capabilities": sorted(caps),
            "duration_days": body.duration_days,
            "membership_epoch": member.epoch,
            "user_epoch": user.auth_epoch,
            "device_epoch": device.epoch,
        }
        a = self.approval(db, "grant", actor.user.id, user.id, payload)
        self.audit(db, actor.user.id, "grant.requested", a.id, request_id, body.project_id)
        return approval_view(a)

    def decide(self, db, actor, approval_id, body: S.Decision, request_id):
        require_root(actor)
        a = db.get(Approval, approval_id)
        if not a:
            raise HubError("APPROVAL_NOT_FOUND", 404)
        if (
            a.version != body.expected_version
            or a.payload_hash != body.expected_hash
            or a.payload_hash != payload_hash(a.payload)
        ):
            raise HubError("APPROVAL_PAYLOAD_CHANGED", 409)
        if a.status != "pending_approval" or a.expires_at <= now():
            if a.status == "pending_approval" and a.expires_at <= now():
                a.status = "expired"
                raise HubError("APPROVAL_NOT_PENDING", 409, persist=True)
            raise HubError("APPROVAL_NOT_PENDING", 409)
        grant = None
        if body.approve:
            user = db.get(User, a.subject_id)
            if not user or user.status != "active":
                raise HubError("SUBJECT_NOT_ELIGIBLE", 409)
            if a.kind == "device":
                device = db.get(Device, uuid.UUID(a.payload["device_id"]))
                if (
                    device.status != "pending"
                    or device.user_id != user.id
                    or device.thumbprint != a.payload["thumbprint"]
                ):
                    raise HubError("SUBJECT_NOT_ELIGIBLE", 409)
                if user.email and not device.email_verified_at:
                    raise HubError("EMAIL_VERIFICATION_REQUIRED", 409)
                device.status, device.approved_by = "approved", actor.user.id
            elif a.kind == "grant":
                p = a.payload
                member = self.membership(db, uuid.UUID(p["project_id"]), user.id)
                device = db.get(Device, uuid.UUID(p["device_id"]))
                project = self.project(db, actor, uuid.UUID(p["project_id"]))
                self.validate_identity(user, device)
                ceiling = ADMIN_CAPS if member and member.role == "administrator" else MEMBER_CAPS
                if (
                    not member
                    or member.status != "active"
                    or member.epoch != p["membership_epoch"]
                    or user.auth_epoch != p["user_epoch"]
                    or device.epoch != p["device_epoch"]
                    or device.user_id != user.id
                    or not set(p["capabilities"]) <= ceiling
                ):
                    raise HubError("APPROVAL_SUBJECT_CHANGED", 409)
                self.revoke_grants(db, project_id=project.id, user_id=user.id, device_id=device.id)
                grant = Grant(
                    project_id=project.id,
                    user_id=user.id,
                    device_id=device.id,
                    approval_id=a.id,
                    capabilities=p["capabilities"],
                    membership_epoch=member.epoch,
                    user_epoch=user.auth_epoch,
                    device_epoch=device.epoch,
                    expires_at=now() + timedelta(days=p["duration_days"]),
                )
                db.add(grant)
                db.flush()
            else:
                raise HubError("UNKNOWN_APPROVAL_KIND", 409)
        a.status, a.decided_by = ("issued" if body.approve else "denied"), actor.user.id
        self.audit(db, actor.user.id, "approval.decided", a.id, request_id, approved=body.approve)
        return {**approval_view(a), "grant_id": str(grant.id) if grant else None}

    @staticmethod
    def revoke_grants(db, **filters):
        statement = update(Grant).where(Grant.status == "active")
        for key, value in filters.items():
            statement = statement.where(getattr(Grant, key) == value)
        db.execute(statement.values(status="revoked"))

    def revoke_member(self, db, actor, project_id, user_id, request_id):
        self.manage_members(db, actor, project_id)
        member = self.membership(db, project_id, user_id)
        if not member:
            raise HubError("MEMBER_NOT_ACCESSIBLE", 404)
        if actor.user.platform_role != "superadmin" and member.role == "administrator":
            raise HubError("SUPERADMIN_REQUIRED")
        member.status, member.epoch = "revoked", member.epoch + 1
        self.revoke_grants(db, project_id=project_id, user_id=user_id)
        db.add(
            Outbox(
                kind="project.access_revoked",
                payload={
                    "project_id": str(project_id),
                    "user_id": str(user_id),
                    "membership_epoch": member.epoch,
                },
            )
        )
        self.audit(db, actor.user.id, "project.member_revoked", user_id, request_id, project_id)
        return {
            "status": "revoked",
            "normal_operations_allowed": False,
            "external_revocation": "pending",
            "closeout_available": False,
        }

    def user_status(self, db, actor, user_id, status, request_id):
        require_root(actor)
        user = db.get(User, user_id)
        if not user or user.platform_role == "superadmin":
            raise HubError("USER_STATUS_NOT_ALLOWED", 409)
        if status == "active" and user.email and not user.email_verified_at:
            raise HubError("EMAIL_VERIFICATION_REQUIRED", 409)
        user.status, user.auth_epoch = status, user.auth_epoch + 1
        self.revoke_grants(db, user_id=user.id)
        db.add(
            Outbox(
                kind="user.status_changed",
                payload={"user_id": str(user.id), "status": status, "auth_epoch": user.auth_epoch},
            )
        )
        self.audit(db, actor.user.id, "user.status_changed", user.id, request_id, status=status)
        return user_view(user)

    def restore_member(self, db, actor, project_id, user_id, request_id):
        require_root(actor)
        self.project(db, actor, project_id)
        member = self.membership(db, project_id, user_id)
        user = db.get(User, user_id)
        if not member or member.status != "revoked" or not user or user.status != "active":
            raise HubError("MEMBER_RESTORATION_NOT_ALLOWED", 409)
        member.status, member.epoch = "active", member.epoch + 1
        self.audit(db, actor.user.id, "project.member_restored", user_id, request_id, project_id)
        return {"status": "active", "grant_required": True}

    def revoke_device(self, db, actor, device_id, request_id):
        require_root(actor)
        device = db.get(Device, device_id)
        if not device or device.id == actor.device.id:
            raise HubError("DEVICE_REVOCATION_NOT_ALLOWED", 409)
        device.status, device.epoch = "revoked", device.epoch + 1
        self.revoke_grants(db, device_id=device.id)
        db.add(Outbox(kind="device.revoked", payload={"device_id": str(device.id), "epoch": device.epoch}))
        self.audit(db, actor.user.id, "device.revoked", device.id, request_id)
        return {"status": "revoked", "normal_operations_allowed": False, "closeout_available": False}

    def idempotent(self, db, actor, scope, key, body, action):
        if not key or not 8 <= len(key) <= 128 or not key.isascii():
            raise HubError("IDEMPOTENCY_KEY_REQUIRED", 400)
        # Secret fields are never persisted as request bodies or hashes of a raw password alone.
        hashed = self.hash(payload_hash(body))
        record = db.scalar(
            select(Idempotency).where(
                Idempotency.actor_id == actor.user.id, Idempotency.scope == scope, Idempotency.key == key
            )
        )
        if record:
            if record.payload_hash != hashed:
                raise HubError("IDEMPOTENCY_PAYLOAD_CHANGED", 409)
            return record.result
        result = action()
        db.add(Idempotency(actor_id=actor.user.id, scope=scope, key=key, payload_hash=hashed, result=result))
        return result
