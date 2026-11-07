import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, StringConstraints, field_validator, model_validator

Login = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewUser(Input):
    login: Login
    password: SecretStr
    email: EmailStr | None = None

    @field_validator("password")
    @classmethod
    def password_length(cls, value):
        if not 12 <= len(value.get_secret_value()) <= 256:
            raise ValueError("Password must contain 12 to 256 characters")
        return value


class Enroll(Input):
    login: Login
    password: SecretStr
    device_name: str = Field(min_length=1, max_length=100)
    os: Literal["windows", "macos"]

    @field_validator("password")
    @classmethod
    def max_password(cls, value):
        if len(value.get_secret_value()) > 256:
            raise ValueError("Password too long")
        return value


class VerifyEmail(Input):
    challenge_id: UUID
    code: str = Field(pattern=r"^\d{8}$")


class StartSession(Input):
    grant_id: UUID | None = None


class NewProject(Input):
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,79}$")
    name: str = Field(min_length=1, max_length=200)


class AssignAdmin(Input):
    user_id: UUID


class GrantRequest(Input):
    project_id: UUID
    user_id: UUID
    device_id: UUID
    capabilities: list[str] = Field(min_length=1, max_length=16)
    duration_days: int = Field(default=30, ge=1, le=90)


class Decision(Input):
    approve: bool
    expected_version: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class UserStatus(Input):
    status: Literal["active", "suspended", "disabled"]


class MemberInvitation(Input):
    user_id: UUID | None = None
    new_user: NewUser | None = None


class Empty(Input):
    pass


class ProgressReport(Input):
    status: Literal["running", "checkpoint", "blocked", "completed"] = "checkpoint"
    summary: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=8000)
    task_reference: str = Field(default="", max_length=128)
    git_commit: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")

    @field_validator("summary")
    @classmethod
    def meaningful_summary(cls, value):
        if not value.strip():
            raise ValueError("A progress summary is required")
        return value.strip()


Harness = Literal["human", "claude-code", "codex", "cursor", "qoder", "workbuddy", "codebuddy", "trae"]


class TaskScope(Input):
    repository: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=80)
    paths: list[str] = Field(min_length=1, max_length=40)

    @field_validator("paths")
    @classmethod
    def relative_paths(cls, values):
        # Scope is an admission declaration, not a substitute for Git permissions.
        return sorted({document_path(value) for value in values})


class NewTask(Input):
    parent_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    acceptance: str = Field(min_length=1, max_length=8000)
    scope: TaskScope

    @model_validator(mode="after")
    def bounded_record(self):
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("Task record exceeds 64 KiB")
        return self

    @field_validator("title", "description", "acceptance")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank value is required")
        return value.strip()


class TaskClaim(Input):
    instance_id: UUID
    harness: Harness


class LeaseInput(Input):
    run_id: UUID
    lease_epoch: int = Field(ge=1)


class WorklogInput(LeaseInput):
    summary: str = Field(min_length=1, max_length=300)
    notes: str = Field(min_length=1, max_length=16000)
    changed_paths: list[str] = Field(default_factory=list, max_length=100)
    tests: str = Field(min_length=1, max_length=8000)
    next_steps: str = Field(min_length=1, max_length=4000)
    outcome: Literal["checkpoint", "blocked", "failed"] = "checkpoint"
    git_commit: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")

    @model_validator(mode="after")
    def bounded_record(self):
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("Worklog record exceeds 64 KiB")
        return self

    @field_validator("summary", "notes", "tests", "next_steps")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank value is required")
        return value.strip()

    @field_validator("changed_paths")
    @classmethod
    def relative_paths(cls, values):
        return sorted({document_path(value) for value in values})


class TaskSubmit(LeaseInput):
    worklog_id: UUID
    expected_version: int = Field(ge=1)


class TaskRelease(LeaseInput):
    reason: str = Field(min_length=1, max_length=1000)


class GateDecision(Input):
    gate: Literal["requirements", "design", "feasibility", "review", "test", "release"]
    approved: bool
    expected_version: int = Field(ge=1)
    worklog_id: UUID | None = None
    evidence: str = Field(min_length=1, max_length=8000)
    override_reason: str = Field(default="", max_length=1000)
    instance_id: UUID
    harness: Harness

    @field_validator("evidence")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Evidence is required")
        return value.strip()


class SiteConfigurationInput(Input):
    expected_version: int = Field(ge=0)
    public_url: str | None = Field(default=None, max_length=300)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_from: EmailStr | None = None
    smtp_username: str = Field(default="", max_length=254)
    smtp_password: SecretStr | None = None
    smtp_transport: Literal["starttls", "tls", "local"] = "starttls"

    @field_validator("public_url")
    @classmethod
    def https_origin(cls, value):
        if not value:
            return None
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path.rstrip("/")
        ):
            raise ValueError("Use an HTTPS origin without a path or credentials")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("Invalid port") from exc
        return value.rstrip("/")

    @field_validator("smtp_host", "smtp_username")
    @classmethod
    def single_line(cls, value):
        if any(c in value for c in "\r\n\0"):
            raise ValueError("Use a single-line value")
        return value.strip()

    @field_validator("smtp_password")
    @classmethod
    def bounded_secret(cls, value):
        if value and len(value.get_secret_value()) > 2048:
            raise ValueError("SMTP password is too long")
        return value


class BootstrapStart(NewUser):
    email: EmailStr
    device_name: str = Field(min_length=1, max_length=100)
    os: Literal["windows", "macos"]


class BootstrapComplete(Input):
    attempt_id: UUID
    code: str = Field(pattern=r"^\d{8}$")


class EmailChangeRequest(Input):
    email: EmailStr
    password: SecretStr

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value):
        if len(value.get_secret_value()) > 256:
            raise ValueError("Password too long")
        return value


def document_path(value):
    if (
        not value
        or len(value) > 800
        or value.startswith("/")
        or "\\" in value
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Use a relative repository path without dot segments")
    return value


class DocumentRepositoryInput(Input):
    expected_version: int = Field(ge=0)
    repository_url: str = Field(max_length=400)
    branch: str = Field(min_length=1, max_length=200)
    include_paths: list[str] = Field(min_length=1, max_length=20)
    token: SecretStr | None = None
    clear_token: bool = False

    @field_validator("repository_url")
    @classmethod
    def github_repository(cls, value):
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})", value
        )
        if not match or match[2] in {".", ".."}:
            raise ValueError("Use https://github.com/owner/repository without query or credentials")
        repository = match[2].removesuffix(".git")
        if not repository or repository in {".", ".."}:
            raise ValueError("Invalid repository name")
        return f"https://github.com/{match[1]}/{repository}"

    @field_validator("branch")
    @classmethod
    def git_branch(cls, value):
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value)
            or ".." in value
            or "//" in value
            or value.endswith(("/", ".", ".lock"))
            or any(p.startswith(".") or p.endswith(".lock") for p in value.split("/"))
        ):
            raise ValueError("Invalid branch name")
        return value

    @field_validator("include_paths")
    @classmethod
    def paths(cls, values):
        return sorted({document_path(v.rstrip("/")) for v in values})

    @field_validator("token")
    @classmethod
    def bounded_token(cls, value):
        if value is not None and (
            not 1 <= len(value.get_secret_value()) <= 2048
            or any(c.isspace() for c in value.get_secret_value())
        ):
            raise ValueError("Invalid repository credential")
        return value


class DocumentSearch(Input):
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=20, ge=1, le=50)
    snapshot_id: UUID | None = None


class DocumentRead(Input):
    path: str = Field(min_length=1, max_length=800)
    snapshot_id: UUID | None = None

    @field_validator("path")
    @classmethod
    def path_valid(cls, value):
        return document_path(value)
