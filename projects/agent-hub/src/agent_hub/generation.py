"""Append-only provenance from the separate server generation worker."""

import uuid
from typing import Literal

from pydantic import Field
from sqlalchemy import BigInteger, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from . import schemas as S
from .models import Base, Identity


class Report(S.Input):
    execution: Literal["server-background"]
    provider: Literal["openai-compatible"]
    model: str = Field(min_length=1, max_length=120)
    response_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_bytes: int = Field(ge=1, le=65536)
    input_tokens: int | None = Field(default=None, ge=0, le=10000000)
    output_tokens: int | None = Field(default=None, ge=0, le=10000000)


class GenerationReceipt(Identity, Base):
    __tablename__ = "generation_receipts"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_repositories.id"))
    distillation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("distillation_jobs.id"))
    guide_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("guide_jobs.id"))
    code_audit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("code_audits.id"))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    response_hash: Mapped[str] = mapped_column(String(64))
    input_bytes: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)


def record(db, identity, row, report, kind):
    if report is None:
        return
    db.flush([row])
    db.add(
        GenerationReceipt(
            project_id=row.project_id,
            repository_id=row.repository_id,
            distillation_id=row.id if kind == "distillation" else None,
            guide_id=row.id if kind == "guide" else None,
            code_audit_id=row.id if kind == "code_audit" else None,
            lease_epoch=row.lease_epoch,
            author_id=identity.user.id,
            device_id=identity.device.id,
            **report.model_dump(exclude={"execution"}),
        )
    )
    db.flush()


def view(db, row, kind):
    key = {"distillation": GenerationReceipt.distillation_id, "guide": GenerationReceipt.guide_id,
           "code_audit": GenerationReceipt.code_audit_id}[kind]
    receipt = db.scalar(
        select(GenerationReceipt)
        .where(key == row.id)
        .order_by(GenerationReceipt.created_at.desc(), GenerationReceipt.id)
        .limit(1)
    )
    if not receipt:
        return None
    return {
        "execution": "server-background",
        "trust": "server-worker-provider-response",
        "provider": receipt.provider,
        "model": receipt.model,
        "input_tokens": receipt.input_tokens,
        "output_tokens": receipt.output_tokens,
        "input_bytes": receipt.input_bytes,
        "response_hash": receipt.response_hash,
        "billing_independently_verified": False,
    }


def policy_hint(db, repository_id, kind):
    from .server_generation import latest

    policy = latest(db, repository_id) if repository_id else None
    return {
        "server_enabled": bool(
            policy
            and policy.content["enabled"]
            and policy.content.get("guides" if kind == "guide" else kind)
        ),
        "policy_version": policy.version if policy else 0,
    }
