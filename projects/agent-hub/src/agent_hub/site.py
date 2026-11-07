"""Service-private site settings. SMTP secrets are sealed before reaching PostgreSQL."""

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .models import SiteConfiguration
from .security import HubError, canonical, now


class SiteSettings:
    def __init__(self, settings):
        self.settings = settings
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-hub.site-configuration.v1"
        ).derive(settings.secret.get_secret_value().encode())
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    def read(self, db):
        row = db.get(SiteConfiguration, "site")
        if row:
            try:
                return json.loads(self.fernet.decrypt(row.encrypted_value.encode())), row.version
            except (InvalidToken, ValueError) as exc:
                raise HubError("CONFIGURATION_UNAVAILABLE", 503) from exc
        cfg = self.settings
        return {
            "public_url": None,
            "smtp_host": cfg.smtp_host,
            "smtp_port": cfg.smtp_port,
            "smtp_from": cfg.smtp_from,
            "smtp_username": cfg.smtp_username,
            "smtp_password": cfg.smtp_password.get_secret_value(),
            "smtp_transport": "tls" if cfg.smtp_ssl else "starttls" if cfg.smtp_starttls else "local",
        }, 0

    def view(self, db):
        data, version = self.read(db)
        return {k: v for k, v in data.items() if k != "smtp_password"} | {
            "version": version,
            "smtp_password_set": bool(data.get("smtp_password")),
            "mail_configured": bool(data.get("smtp_host") and data.get("smtp_from")),
            "active_origin": self.settings.public_url,
        }

    def write(self, db, body, actor_id=None):
        previous, version = self.read(db)
        if body.expected_version != version:
            raise HubError("CONFIGURATION_VERSION_CONFLICT", 409)
        data = body.model_dump(mode="python", exclude={"expected_version", "smtp_password"})
        data["smtp_from"] = str(body.smtp_from) if body.smtp_from else ""
        data["smtp_password"] = (
            body.smtp_password.get_secret_value()
            if body.smtp_password is not None
            else previous.get("smtp_password", "")
        )
        if data["smtp_transport"] == "local" and data["smtp_host"] not in {
            "",
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise HubError("SMTP_TLS_REQUIRED", 422)
        encrypted = self.fernet.encrypt(canonical(data).encode()).decode()
        row = db.get(SiteConfiguration, "site")
        if not row:
            row = SiteConfiguration(
                key="site", encrypted_value=encrypted, version=version + 1, updated_by=actor_id
            )
            db.add(row)
        else:
            row.encrypted_value, row.version, row.updated_by = encrypted, version + 1, actor_id
            row.updated_at = now()
        db.flush()
        return self.view(db)
