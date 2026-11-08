import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy import Identity as AutoIdentity
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Identity, Base):
    __tablename__ = "users"
    login: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    platform_role: Mapped[str] = mapped_column(String(24), default="member")
    admin_eligible: Mapped[bool] = mapped_column(default=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="active")
    auth_epoch: Mapped[int] = mapped_column(Integer, default=1)


class Device(Identity, Base):
    __tablename__ = "devices"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    public_jwk: Mapped[dict] = mapped_column(JSONB)
    thumbprint: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    os: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)


class Enrollment(Identity, Base):
    __tablename__ = "enrollments"
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailChallenge(Identity, Base):
    __tablename__ = "email_challenges"
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(String(16), default="device", server_default="device")
    user_epoch: Mapped[int | None] = mapped_column(Integer)


class Approval(Identity, Base):
    __tablename__ = "approval_requests"
    kind: Mapped[str] = mapped_column(String(24))
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="pending_approval")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Project(Identity, Base):
    __tablename__ = "projects"
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)


class Membership(Identity, Base):
    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(24), default="member")
    status: Mapped[str] = mapped_column(String(24), default="active")
    epoch: Mapped[int] = mapped_column(Integer, default=1)


class Grant(Identity, Base):
    __tablename__ = "access_grants"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    capabilities: Mapped[list] = mapped_column(JSONB)
    membership_epoch: Mapped[int] = mapped_column(Integer)
    user_epoch: Mapped[int] = mapped_column(Integer)
    device_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSession(Identity, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    grant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_grants.id"))
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    access_digest: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_epoch: Mapped[int] = mapped_column(Integer)
    device_epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="active")
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProofReplay(Base):
    __tablename__ = "proof_replays"
    thumbprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Audit(Identity, Base):
    __tablename__ = "audit_events"
    __table_args__ = {"implicit_returning": False}
    __mapper_args__ = {"eager_defaults": False}
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    action: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class Outbox(Identity, Base):
    __tablename__ = "outbox_events"
    kind: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), default="pending")


class Idempotency(Identity, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("actor_id", "scope", "key"),)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(300))
    key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)


class SiteConfiguration(Base):
    __tablename__ = "site_configuration"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BootstrapAttempt(Identity, Base):
    __tablename__ = "bootstrap_attempts"
    login: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(254))
    public_jwk: Mapped[dict] = mapped_column(JSONB)
    thumbprint: Mapped[str] = mapped_column(String(64), index=True)
    device_name: Mapped[str] = mapped_column(String(100))
    os: Mapped[str] = mapped_column(String(16))
    configuration_version: Mapped[int] = mapped_column(Integer)
    challenge_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ProjectProgress(Base):
    __tablename__ = "project_progress"
    sequence: Mapped[int] = mapped_column(BigInteger, AutoIdentity(), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    status: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    task_reference: Mapped[str] = mapped_column(String(128), default="")
    git_commit: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40), default="report", server_default="report")
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worklog_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Identity, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("id", "project_id"),
        ForeignKeyConstraint(["parent_id", "project_id"], ["tasks.id", "tasks.project_id"]),
        Index("ix_tasks_project_created", "project_id", "created_at", "id"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    acceptance: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSONB)
    execution_mode: Mapped[str] = mapped_column(String(24), default="observed", server_default="declaration")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    admission_step: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class TaskRun(Identity, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("id", "task_id", "project_id"),
        ForeignKeyConstraint(["task_id", "project_id"], ["tasks.id", "tasks.project_id"]),
        Index("ix_task_runs_task", "project_id", "task_id", "created_at"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    harness: Mapped[str] = mapped_column(String(24))
    role: Mapped[str] = mapped_column(String(24))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Worklog(Identity, Base):
    __tablename__ = "worklogs"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "task_id", "project_id"],
                             ["task_runs.id", "task_runs.task_id", "task_runs.project_id"]),
        Index("ix_worklogs_task", "project_id", "task_id", "created_at", "id"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text)
    changed_paths: Mapped[list] = mapped_column(JSONB)
    tests: Mapped[str] = mapped_column(Text)
    next_steps: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(24))
    git_commit: Mapped[str | None] = mapped_column(String(64))
    receipt_hash: Mapped[str] = mapped_column(String(64))
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    change_evidence: Mapped[dict | None] = mapped_column(JSONB)
    evidence_hash: Mapped[str | None] = mapped_column(String(64))


class ProjectPolicy(Base):
    __tablename__ = "project_policies"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkContext(Identity, Base):
    __tablename__ = "work_contexts"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "task_id", "project_id"],
                             ["task_runs.id", "task_runs.task_id", "task_runs.project_id"]),
        Index("ix_contexts_run", "project_id", "run_id", "created_at"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[int] = mapped_column(Integer)
    task_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskDecision(Identity, Base):
    __tablename__ = "task_decisions"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_id"], ["tasks.id", "tasks.project_id"]),
        Index("ix_task_decisions_task", "project_id", "task_id", "created_at"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worklog_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    gate: Mapped[str] = mapped_column(String(24))
    approved: Mapped[bool] = mapped_column()
    evidence: Mapped[str] = mapped_column(Text)
    override_reason: Mapped[str] = mapped_column(Text)
    task_version: Mapped[int] = mapped_column(Integer)


class DocumentRepository(Base):
    __tablename__ = "document_repositories"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    repository_url: Mapped[str] = mapped_column(String(400))
    branch: Mapped[str] = mapped_column(String(200))
    include_paths: Mapped[list] = mapped_column(JSONB)
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    git_commit: Mapped[str | None] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)


class DocumentSync(Identity, Base):
    __tablename__ = "document_syncs"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    configuration_version: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    grant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_grants.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


class ProjectDocument(Base):
    __tablename__ = "project_documents"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    path: Mapped[str] = mapped_column(String(800), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)


Index(
    "one_superadministrator",
    User.platform_role,
    unique=True,
    postgresql_where=User.platform_role == "superadmin",
)
