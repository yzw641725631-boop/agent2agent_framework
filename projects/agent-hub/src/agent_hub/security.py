import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from .models import ProofReplay, RateBucket

PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
# Argon2 uses about 64 MiB per operation; the API container has a 512 MiB limit.
PASSWORD_SLOTS = BoundedSemaphore(2)


class HubError(Exception):
    def __init__(self, code: str, status: int = 403, *, persist: bool = False, **details):
        self.code, self.status, self.persist, self.details = code, status, persist, details


def now():
    return datetime.now(UTC)


def b64(value: bytes):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.get_secret_value() if isinstance(item, SecretStr) else str(item),
    )


def payload_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def digest(secret: str, value: str):
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def verify_password(encoded: str | None, password: str):
    if not PASSWORD_SLOTS.acquire(timeout=0.1):
        raise HubError("SERVICE_BUSY", 503)
    try:
        return PASSWORDS.verify(encoded or DUMMY_HASH, password) and encoded is not None
    except (VerificationError, InvalidHashError):
        return False
    finally:
        PASSWORD_SLOTS.release()


def hash_password(password: str):
    if not PASSWORD_SLOTS.acquire(timeout=0.1):
        raise HubError("SERVICE_BUSY", 503)
    try:
        return PASSWORDS.hash(password)
    finally:
        PASSWORD_SLOTS.release()


def public_key(jwk: dict):
    if set(jwk) != {"kty", "crv", "x", "y"} or jwk["kty"] != "EC" or jwk["crv"] != "P-256":
        raise HubError("INVALID_DEVICE_KEY", 401)
    try:
        key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
        normalized = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key))
        clean = {k: normalized[k] for k in ("kty", "crv", "x", "y")}
        if clean != jwk:
            raise ValueError("Noncanonical key")
        return key, b64(hashlib.sha256(canonical(clean).encode()).digest())
    except (ValueError, TypeError, KeyError) as exc:
        raise HubError("INVALID_DEVICE_KEY", 401) from exc


def check_proof(factory, settings, proof: str, method: str, path: str, token: str | None = None):
    if not proof or len(proof) > 4096:
        raise HubError("DEVICE_PROOF_REQUIRED", 401)
    try:
        header = jwt.get_unverified_header(proof)
        if set(header) != {"typ", "alg", "jwk"} or header["typ"] != "dpop+jwt" or header["alg"] != "ES256":
            raise HubError("INVALID_DEVICE_PROOF", 401)
        key, thumbprint = public_key(header["jwk"])
        claims = jwt.decode(
            proof,
            key,
            algorithms=["ES256"],
            options={
                "require": ["jti", "htm", "htu", "iat"],
                "verify_aud": False,
            },
        )
        expected_url = settings.public_url.rstrip("/") + path
        if claims["htm"] != method.upper() or claims["htu"] != expected_url:
            raise HubError("INVALID_DEVICE_PROOF", 401)
        if type(claims["iat"]) is not int or abs(now().timestamp() - claims["iat"]) > settings.proof_seconds:
            raise HubError("EXPIRED_DEVICE_PROOF", 401)
        jti = claims["jti"]
        if not isinstance(jti, str) or not 16 <= len(jti) <= 128 or not jti.isascii():
            raise HubError("INVALID_DEVICE_PROOF", 401)
        if token is not None and not hmac.compare_digest(
            str(claims.get("ath", "")), b64(hashlib.sha256(token.encode()).digest())
        ):
            raise HubError("INVALID_DEVICE_PROOF", 401)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HubError("INVALID_DEVICE_PROOF", 401) from exc
    # Independent commit means rejected business transactions cannot resurrect a consumed proof.
    with factory.begin() as replay_db:
        replay_db.execute(delete(ProofReplay).where(ProofReplay.expires_at < now()))
        inserted = replay_db.execute(
            insert(ProofReplay)
            .values(
                thumbprint=thumbprint,
                jti=jti,
                expires_at=now() + timedelta(seconds=settings.proof_seconds * 2),
            )
            .on_conflict_do_nothing()
            .returning(ProofReplay.jti)
        ).scalar_one_or_none()
    if inserted is None:
        raise HubError("DEVICE_PROOF_REPLAY", 401)
    return header["jwk"], thumbprint


def rate_limit(factory, secret: str, scope: str, limit: int, seconds: int):
    key = digest(secret, scope)
    with factory.begin() as db:
        # Bounded cleanup avoids unbounded key retention and competing full-table deletes.
        expired = (
            select(RateBucket.key)
            .where(RateBucket.resets_at < now())
            .order_by(RateBucket.resets_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        db.execute(delete(RateBucket).where(RateBucket.key.in_(expired)))
        db.execute(
            insert(RateBucket)
            .values(key=key, count=0, resets_at=now() + timedelta(seconds=seconds))
            .on_conflict_do_nothing()
        )
        row = db.get(RateBucket, key, with_for_update=True)
        if row.resets_at <= now():
            row.count, row.resets_at = 0, now() + timedelta(seconds=seconds)
        row.count = min(row.count + 1, limit + 1)
        exceeded = row.count > limit
    if exceeded:
        raise HubError("RATE_LIMITED", 429)
