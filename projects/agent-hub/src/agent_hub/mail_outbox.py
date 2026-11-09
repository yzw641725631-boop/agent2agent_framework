"""Encrypted mail intents; SMTP is never called while a database transaction is open."""

import base64
import json
import uuid
from datetime import datetime, timedelta

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, or_, select

from .db import coordinate
from .models import AccountChallenge, BootstrapAttempt, Device, EmailChallenge, Outbox
from .security import HubError, now
from .site import SiteSettings


def cipher(settings):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-hub.mail-outbox.v1").derive(
        settings.secret.get_secret_value().encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def enqueue(db, settings, email, *, code=None, reference=None, reference_kind=None, expires_at=None):
    job = Outbox(
        id=uuid.uuid4(),
        kind="mail.delivery",
        state="pending",
        payload={
            "sealed": cipher(settings).encrypt(json.dumps({"email": email, "code": code}).encode()).decode(),
            "expires_at": (expires_at or now() + timedelta(minutes=5)).isoformat(),
            "reference": str(reference) if reference else None,
            "reference_kind": reference_kind,
            "attempts": 0,
        },
    )
    db.add(job)
    db.flush()
    db.info.setdefault("mail_deliveries", []).append(job.id)
    return job.id


class MailOutbox:
    def __init__(self, settings, factory, mailer):
        self.settings, self.sessions, self.mailer = settings, factory, mailer
        self.site = SiteSettings(settings)

    @staticmethod
    def reference(db, payload):
        model = {"bootstrap": BootstrapAttempt, "challenge": EmailChallenge, "account": AccountChallenge}.get(
            payload.get("reference_kind")
        )
        return db.get(model, uuid.UUID(payload["reference"])) if model else None

    def deliver(self, job_id, *, raise_errors=False):
        with self.sessions.begin() as db:
            job = db.scalar(
                select(Outbox)
                .where(Outbox.id == job_id, Outbox.kind == "mail.delivery")
                .with_for_update(skip_locked=True)
            )
            if job is None or job.state == "sent":
                return
            payload = dict(job.payload)
            if job.state not in {"pending", "running"}:
                return
            if job.state == "running" and datetime.fromisoformat(payload["lease_until"]) > now():
                return
            reference = self.reference(db, payload)
            stale = payload.get("reference") and (
                not reference or reference.expires_at <= now() or getattr(reference, "consumed_at", None)
            )
            if stale or datetime.fromisoformat(payload["expires_at"]) <= now() or payload["attempts"] >= 3:
                job.state, job.payload = "cancelled", {"error_code": "MAIL_INTENT_EXPIRED"}
                return
            lease = str(uuid.uuid4())
            payload.update(
                attempts=payload["attempts"] + 1,
                lease_id=lease,
                lease_until=(now() + timedelta(minutes=3)).isoformat(),
            )
            job.state, job.payload = "running", payload
            configuration = self.site.read(db)[0]

        # No connection is checked out while network I/O is in progress.
        failure = None
        try:
            message = json.loads(cipher(self.settings).decrypt(payload["sealed"].encode()))
            if message["code"] is None:
                self.mailer.send_test(message["email"], configuration)
            else:
                self.mailer.send_code(message["email"], message["code"], configuration)
        except HubError as exc:
            failure = exc
        except Exception:
            failure = HubError("MAIL_DELIVERY_FAILED", 503)

        with self.sessions.begin() as db:
            # Failure cleanup follows the same identity/recovery barrier as code
            # verification. A sender must not delete a concurrently consumed code.
            if failure and payload.get("reference"):
                if payload.get("reference_kind") == "bootstrap":
                    coordinate(db)
                elif payload.get("reference_kind") == "challenge":
                    user_id = db.scalar(
                        select(Device.user_id)
                        .join(EmailChallenge, EmailChallenge.device_id == Device.id)
                        .where(EmailChallenge.id == uuid.UUID(payload["reference"]))
                    )
                    if user_id:
                        coordinate(db, user_ids=[user_id])
            job = db.get(Outbox, job_id, with_for_update=True)
            if job.state != "running" or job.payload.get("lease_id") != lease:
                return
            job.state = "failed" if failure else "sent"
            job.payload = {key: payload[key] for key in ("reference", "reference_kind", "attempts")}
            if failure:
                job.payload = {**job.payload, "error_code": failure.code}
                # A failed request did not return its challenge ID. Allow a clean resend;
                # the persisted per-user mail budget still limits repeated attempts.
                model = {"bootstrap": BootstrapAttempt, "challenge": EmailChallenge}.get(
                    payload.get("reference_kind")
                )
                if model:
                    statement = delete(model).where(model.id == uuid.UUID(payload["reference"]))
                    if model is EmailChallenge:
                        statement = statement.where(model.consumed_at.is_(None))
                    db.execute(statement)
        if failure and raise_errors:
            raise failure

    def recover_once(self):
        # Give the initiating request time to deliver after its commit. A crashed request
        # leaves a durable intent; a crashed sender is retried after its lease expires.
        with self.sessions() as db:
            ids = list(
                db.scalars(
                    select(Outbox.id)
                    .where(
                        Outbox.kind == "mail.delivery",
                        or_(Outbox.state == "pending", Outbox.state == "running"),
                        Outbox.created_at < now() - timedelta(seconds=30),
                    )
                    .order_by(Outbox.created_at)
                    .limit(10)
                )
            )
        for job_id in ids:
            self.deliver(job_id)
