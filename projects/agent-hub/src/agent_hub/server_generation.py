"""Superadmin-authorized, repository-scoped server generation configuration."""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Depends, Query, Request
from pydantic import Field, SecretStr, model_validator
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import repositories
from . import schemas as S
from .generation_provider import endpoint
from .models import Base, Identity
from .security import HubError, canonical
from .service import require_root


class ProviderConfiguration(Base):
    __tablename__ = "generation_configuration"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    encrypted_value: Mapped[str] = mapped_column(Text)


class RepositoryPolicy(Base):
    __tablename__ = "generation_policies"
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_repositories.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    instance_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content: Mapped[dict] = mapped_column(JSONB)


class Call(Identity, Base):
    __tablename__ = "generation_calls"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_repositories.id"))
    policy_version: Mapped[int] = mapped_column(Integer)
    provider_version: Mapped[int] = mapped_column(Integer)
    provider_model: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[uuid.UUID]
    state: Mapped[str] = mapped_column(String(20))
    lease_token: Mapped[uuid.UUID]
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    instance_id: Mapped[uuid.UUID]
    job_version: Mapped[int] = mapped_column(Integer)
    job_lease_epoch: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    input_bytes: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    encrypted_result: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetryRequest(Identity, Base):
    __tablename__ = "generation_retry_requests"
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_calls.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    allow_new_provider_call: Mapped[bool]
    reason: Mapped[str] = mapped_column(String(2000))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(100))


class ConfigurationInput(S.Input):
    expected_version: int = Field(ge=0)
    endpoint: str = Field(min_length=1, max_length=1000)
    model: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,119}$")
    api_key: SecretStr | None = None
    max_input_bytes: int = Field(default=48000, ge=16000, le=65536)
    max_output_tokens: int = Field(default=3000, ge=256, le=8192)
    daily_token_limit: int = Field(default=100000, ge=1000, le=10000000)
    daily_call_limit: int = Field(default=10, ge=1, le=1000)
    timeout_seconds: int = Field(default=60, ge=5, le=120)
    token_parameter: str = Field(
        default="max_completion_tokens", pattern=r"^(max_tokens|max_completion_tokens)$"
    )
    reason: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def valid_key(self):
        key = self.api_key.get_secret_value() if self.api_key else None
        if key is not None and (not key.strip() or len(key) > 4000 or "\n" in key or "\r" in key):
            raise ValueError("A nonblank bounded API key is required")
        return self


class PolicyInput(S.Input):
    expected_version: int = Field(ge=0)
    enabled: bool = False
    distillation: bool = True
    guides: bool = False
    reason: str = Field(min_length=10, max_length=2000)


class RetryInput(S.Input):
    allow_new_provider_call: bool = False
    reason: str = Field(min_length=10, max_length=2000)


class Configuration:
    def __init__(self, settings):
        self.settings = settings
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-hub.server-generation.v1"
        ).derive(settings.secret.get_secret_value().encode())
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, value):
        return self.fernet.encrypt(canonical(value).encode()).decode()

    def open(self, value):
        try:
            return json.loads(self.fernet.decrypt(value.encode()))
        except Exception:
            raise HubError("GENERATION_CONFIGURATION_UNAVAILABLE", 503) from None

    def read(self, db):
        row = db.get(ProviderConfiguration, "provider")
        return (self.open(row.encrypted_value), row.version) if row else ({}, 0)

    def view(self, db):
        value, version = self.read(db)
        return {
            **{k: v for k, v in value.items() if k != "api_key"},
            "version": version,
            "api_key_set": bool(value.get("api_key")),
            "execution_location": "server",
            "credential_returned": False,
        }

    def write(self, db, body):
        previous, version = self.read(db)
        if version != body.expected_version:
            raise HubError("GENERATION_CONFIGURATION_VERSION_CONFLICT", 409)
        endpoint(body.endpoint, test=self.settings.environment == "test")
        key = body.api_key.get_secret_value() if body.api_key else previous.get("api_key")
        if not key or body.endpoint != previous.get("endpoint") and previous and body.api_key is None:
            raise HubError("GENERATION_NEW_ENDPOINT_KEY_REQUIRED", 422)
        value = body.model_dump(mode="json", exclude={"expected_version", "api_key", "reason"})
        value["api_key"] = key
        row = db.get(ProviderConfiguration, "provider")
        if row:
            row.version, row.encrypted_value = version + 1, self.seal(value)
        else:
            db.add(ProviderConfiguration(key="provider", version=1, encrypted_value=self.seal(value)))
        db.flush()
        return self.view(db)


def latest(db, repository_id):
    return db.scalar(
        select(RepositoryPolicy)
        .where(RepositoryPolicy.repository_id == repository_id)
        .order_by(RepositoryPolicy.version.desc())
        .limit(1)
    )


def policy_view(row):
    return (
        {"version": row.version, **row.content}
        if row
        else {
            "version": 0,
            "enabled": False,
            "distillation": True,
            "guides": False,
        }
    )


def call_view(row):
    return {k: str(getattr(row, k)) for k in ("id", "repository_id", "job_id")} | {
        "kind": row.kind,
        "state": row.state,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "reserved_tokens": row.reserved_tokens,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "model": row.provider_model,
        "has_saved_result": bool(row.encrypted_result),
        "execution_location": "server",
        "billing_independently_verified": False,
    }


def install(app, hub, actor, mutate, dependency):
    DB = Annotated[object, Depends(dependency, scope="function")]
    config = Configuration(hub.settings)

    @app.get("/api/admin/generation")
    def read_configuration(request: Request, db: DB):
        require_root(actor(request, db))
        return config.view(db)

    @app.put("/api/admin/generation")
    def write_configuration(body: ConfigurationInput, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        # Do not persist an idempotency body containing the API key.
        result = config.write(db, body)
        hub.audit(
            db,
            identity.user.id,
            "generation.provider_configured",
            result["version"],
            request.state.request_id,
            reason=body.reason,
        )
        return result

    @app.get("/api/projects/{project_id}/repositories/{repository_id}/generation")
    def read_policy(project_id: uuid.UUID, repository_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        repositories.require(db, project_id, repository_id)
        return policy_view(latest(db, repository_id))

    @app.put("/api/projects/{project_id}/repositories/{repository_id}/generation")
    def write_policy(
        project_id: uuid.UUID, repository_id: uuid.UUID, body: PolicyInput, request: Request, db: DB
    ):
        identity = actor(request, db)
        require_root(identity)
        hub.project(db, identity, project_id, "tasks:publish")
        repositories.require(db, project_id, repository_id)

        def save():
            current = latest(db, repository_id)
            version = current.version if current else 0
            if version != body.expected_version:
                raise HubError("GENERATION_POLICY_VERSION_CONFLICT", 409)
            if body.enabled and not config.read(db)[0]:
                raise HubError("GENERATION_NOT_CONFIGURED", 409)
            row = RepositoryPolicy(
                project_id=project_id,
                repository_id=repository_id,
                version=version + 1,
                author_id=identity.user.id,
                device_id=identity.device.id,
                instance_id=uuid.uuid4(),
                content=body.model_dump(exclude={"expected_version"}),
            )
            db.add(row)
            hub.audit(
                db,
                identity.user.id,
                "generation.repository_authorized",
                repository_id,
                request.state.request_id,
                project_id,
                reason=body.reason,
                enabled=body.enabled,
            )
            db.flush()
            return policy_view(row)

        return mutate(request, db, identity, body, save)

    @app.get("/api/projects/{project_id}/generation-calls")
    def calls(
        project_id: uuid.UUID,
        request: Request,
        db: DB,
        repository_id: uuid.UUID | None = None,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(20, ge=1, le=100),
    ):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        statement = select(Call).where(Call.project_id == project_id)
        if repository_id:
            repositories.require(db, project_id, repository_id)
            statement = statement.where(Call.repository_id == repository_id)
        rows = list(
            db.scalars(statement.order_by(Call.created_at.desc(), Call.id).offset(offset).limit(limit + 1))
        )
        items = []
        for row in rows[:limit]:
            item = call_view(row)
            retry = db.scalar(
                select(RetryRequest)
                .where(RetryRequest.call_id == row.id)
                .order_by(RetryRequest.created_at.desc(), RetryRequest.id.desc())
                .limit(1)
            )
            item["retry"] = {"state": retry.state, "error_code": retry.error_code} if retry else None
            items.append(item)
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_offset": offset + min(len(rows), limit),
            "execution_location": "server",
        }

    @app.post("/api/projects/{project_id}/generation-calls/{call_id}/retry")
    def retry_call(project_id: uuid.UUID, call_id: uuid.UUID, body: RetryInput, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        hub.project(db, identity, project_id, "tasks:publish")
        row = db.scalar(select(Call).where(Call.id == call_id, Call.project_id == project_id))
        if not row:
            raise HubError("GENERATION_CALL_NOT_ACCESSIBLE", 404)
        repositories.require(db, project_id, row.repository_id)

        def save():
            if row.state not in {"blocked", "uncertain"}:
                raise HubError("GENERATION_CALL_NOT_RETRYABLE", 409)
            if not row.encrypted_result and not body.allow_new_provider_call:
                raise HubError("GENERATION_PAID_RETRY_APPROVAL_REQUIRED", 409)
            if db.scalar(
                select(RetryRequest.id).where(RetryRequest.call_id == row.id, RetryRequest.state == "pending")
            ):
                raise HubError("GENERATION_RETRY_ALREADY_PENDING", 409)
            retry = RetryRequest(
                call_id=row.id,
                user_id=identity.user.id,
                device_id=identity.device.id,
                allow_new_provider_call=body.allow_new_provider_call,
                reason=body.reason,
            )
            db.add(retry)
            db.flush()
            hub.audit(
                db,
                identity.user.id,
                "generation.retry_approved",
                row.id,
                request.state.request_id,
                project_id,
                allow_new_provider_call=body.allow_new_provider_call,
                reason=body.reason,
            )
            return {
                "id": str(retry.id),
                "state": retry.state,
                "new_provider_call_authorized": body.allow_new_provider_call,
            }

        return mutate(request, db, identity, body, save)
