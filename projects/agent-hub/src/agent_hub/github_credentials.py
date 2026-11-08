"""Server-only GitHub credentials, narrowed to one registered repository per request."""

import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from .documents import token_cipher
from .models import SiteConfiguration
from .security import HubError, now
from .service import require_root


def app_configuration(hub, db):
    row = db.execute(text("SELECT encrypted_value,version FROM hub.github_app_configuration()")).first()
    if not row:
        return {}, 0
    try:
        return json.loads(hub.site.fernet.decrypt(row.encrypted_value.encode())), row.version
    except Exception as exc:
        raise HubError("GITHUB_APP_CONFIGURATION_UNAVAILABLE", 503) from exc


def app_view(hub, db):
    data, version = app_configuration(hub, db)
    return {
        "version": version,
        "app_id": data.get("app_id"),
        "private_key_set": bool(data.get("private_key")),
    }


def configure_app(hub, db, actor, body, request_id):
    require_root(actor)
    previous, version = app_configuration(hub, db)
    if version != body.expected_version:
        raise HubError("CONFIGURATION_VERSION_CONFLICT", 409)
    key = body.private_key.get_secret_value() if body.private_key else previous.get("private_key", "")
    if (
        not key
        or len(key) > 16384
        or previous.get("app_id", body.app_id) != body.app_id
        and body.private_key is None
    ):
        raise HubError("GITHUB_APP_KEY_REQUIRED", 422)
    try:
        parsed = serialization.load_pem_private_key(key.encode(), password=None)
        if not isinstance(parsed, rsa.RSAPrivateKey) or parsed.key_size < 2048:
            raise ValueError()
    except Exception as exc:
        raise HubError("GITHUB_APP_KEY_INVALID", 422) from exc
    encrypted = hub.site.fernet.encrypt(
        json.dumps({"app_id": body.app_id, "private_key": key}).encode()
    ).decode()
    row = db.get(SiteConfiguration, "github-app")
    if not row:
        row = SiteConfiguration(
            key="github-app", encrypted_value=encrypted, version=version + 1, updated_by=actor.user.id
        )
        db.add(row)
    else:
        row.encrypted_value, row.version, row.updated_by, row.updated_at = (
            encrypted,
            version + 1,
            actor.user.id,
            now(),
        )
    db.flush()
    hub.audit(db, actor.user.id, "github_app.configured", "github-app", request_id, version=row.version)
    return app_view(hub, db)


def credential(hub, db, repository, *, write=False, transport=None):
    if repository.auth_mode == "public":
        if write:
            raise HubError("REPOSITORY_WRITE_CREDENTIAL_REQUIRED", 409)
        return None
    if repository.auth_mode == "token":
        try:
            return token_cipher(hub.settings).decrypt(repository.encrypted_token.encode()).decode()
        except Exception as exc:
            raise HubError("REPOSITORY_CREDENTIAL_UNAVAILABLE", 503) from exc
    data, _ = app_configuration(hub, db)
    if not data.get("private_key") or not repository.installation_id:
        raise HubError("GITHUB_APP_NOT_CONFIGURED", 409)
    timestamp = int(time.time())
    assertion = jwt.encode(
        {"iat": timestamp - 60, "exp": timestamp + 540, "iss": str(data["app_id"])},
        data["private_key"],
        algorithm="RS256",
    )
    try:
        with httpx.Client(transport=transport, timeout=15, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                f"https://api.github.com/app/installations/{repository.installation_id}/access_tokens",
                headers={
                    "Authorization": "Bearer " + assertion,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                json={
                    "repositories": [repository.repository_url.rsplit("/", 1)[1]],
                    "permissions": {"contents": "write" if write else "read"},
                },
            )
            if response.status_code != 201 or len(response.content) > 2 * 1024 * 1024:
                raise HubError("GITHUB_APP_AUTHORIZATION_FAILED", 502)
            result = response.json().get("token")
            if (
                not isinstance(result, str)
                or not 1 <= len(result) <= 8192
                or any(c.isspace() for c in result)
            ):
                raise HubError("GITHUB_APP_AUTHORIZATION_FAILED", 502)
            return result
    except HubError:
        raise
    except Exception as exc:
        raise HubError("GITHUB_APP_AUTHORIZATION_FAILED", 502) from exc
