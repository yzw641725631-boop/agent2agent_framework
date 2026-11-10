import json
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

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


class RepositoryPermission(Input):
    repository_id: UUID
    capabilities: list[Literal["code:read", "code:write", "documents:read"]] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def read_before_write(self):
        self.capabilities = sorted(set(self.capabilities))
        if "code:write" in self.capabilities and "code:read" not in self.capabilities:
            raise ValueError("Writing code requires code:read")
        return self


class GrantRequest(Input):
    project_id: UUID
    user_id: UUID
    device_id: UUID
    capabilities: list[str] = Field(min_length=1, max_length=32)
    repository_permissions: list[RepositoryPermission] = Field(default_factory=list, max_length=100)
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
    repository_id: UUID | None = None
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


class GuidePlan(Input):
    kind: Literal["none", "user", "api", "operations"] = "none"
    module: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.kind != "none" and not self.module.strip():
            raise ValueError("Specify the feature or module for the operation guide")
        return self


class NewTask(Input):
    repository_id: UUID | None = None
    parent_id: UUID | None = None
    depends_on: list[UUID] = Field(default_factory=list, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    acceptance: str = Field(min_length=1, max_length=8000)
    scope: TaskScope
    execution_mode: Literal["observed", "declaration"] = "observed"
    execution_role: Literal["project-manager", "architect", "designer", "developer"] = "developer"
    role_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    workflow: Literal["standard", "compact", "strict"] = "standard"
    workflow_reason: str = Field(default="", max_length=1000)
    documentation: GuidePlan = Field(default_factory=GuidePlan)

    @model_validator(mode="after")
    def bounded_record(self):
        if self.execution_mode == "observed" and any(
            unicodedata.normalize("NFC", value) != value or ":" in value
            or any(part.endswith((".", " ")) for part in value.split("/")) for value in self.scope.paths
        ):
            raise ValueError("Code task scope must use stable, relative NFC paths")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("Task record exceeds 64 KiB")
        if self.workflow != "standard" and len(self.workflow_reason.strip()) < 10:
            raise ValueError("An explicit workflow selection reason of at least 10 characters is required")
        return self

    @field_validator("title", "description", "acceptance")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank value is required")
        return value.strip()


class UsagePolicyInput(Input):
    expected_version: int = Field(ge=0)
    mode: Literal["observe", "pause-managed-work"] = "observe"
    monthly_reported_token_limit: int = Field(default=0, ge=0, le=1000000000000)
    change_reason: str = Field(min_length=10, max_length=1000)


class UsageReport(Input):
    repository_id: UUID | None = None
    task_id: UUID
    run_id: UUID
    reference_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    input_tokens: int = Field(ge=0, le=10000000)
    output_tokens: int = Field(ge=0, le=10000000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10000000)
    reported_cost_micro_usd: int | None = Field(default=None, ge=0, le=1000000000)

    @model_validator(mode="after")
    def cached_is_input(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input must be included in input tokens")
        return self


class RetrievalCase(Input):
    query: str = Field(min_length=1, max_length=200)
    expected_card_ids: list[UUID] = Field(min_length=1, max_length=20)

    @field_validator("expected_card_ids")
    @classmethod
    def distinct_cards(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate expected memory cards")
        return values


class RetrievalEvaluationInput(Input):
    repository_id: UUID | None = None
    cases: list[RetrievalCase] = Field(min_length=1, max_length=10)
    k: int = Field(default=5, ge=1, le=20)
    search_mode: Literal["hybrid", "keyword"] = "hybrid"


class RoleProfile(Input):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    base_role: Literal["project-manager", "architect", "designer", "developer"]
    responsibilities: list[str] = Field(min_length=1, max_length=12)
    outputs: list[str] = Field(min_length=1, max_length=12)
    enabled: bool = True

    @field_validator("responsibilities", "outputs")
    @classmethod
    def bounded_items(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("Use nonblank guidance of at most 500 characters per item")
        return [value.strip() for value in values]


class CollaborationInput(Input):
    expected_version: int = Field(ge=0)
    profiles: list[RoleProfile] = Field(default_factory=list, max_length=32)
    allow_compact: bool = False
    require_guides: bool | None = None
    change_reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def bounded_profiles(self):
        if len({p.key for p in self.profiles}) != len(self.profiles):
            raise ValueError("Duplicate role profile keys")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 65536:
            raise ValueError("Collaboration settings exceed 64 KiB")
        return self


class TaskWorkflowChange(Input):
    expected_version: int = Field(ge=1)
    workflow: Literal["standard", "compact", "strict"]
    reason: str = Field(min_length=10, max_length=1000)


class TaskDependencies(Input):
    expected_version: int = Field(ge=1)
    depends_on: list[UUID] = Field(max_length=40)
    reason: str = Field(min_length=10, max_length=1000)


class TaskClaim(Input):
    instance_id: UUID
    harness: Harness


class AgentRegistration(TaskClaim):
    repository_id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    roles: list[Literal["project-manager", "architect", "designer", "developer"]] = Field(min_length=1, max_length=4)
    role_profiles: list[str] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=0)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Use a nonblank single-line name")
        return value.strip()

    @field_validator("roles")
    @classmethod
    def distinct(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate roles")
        return sorted(values)

    @field_validator("role_profiles")
    @classmethod
    def distinct_profiles(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value) for value in values):
            raise ValueError("Use distinct valid role profile keys")
        return sorted(values)


class AgentHeartbeat(Input):
    expected_version: int = Field(ge=1)


class AgentDispatchInput(Input):
    task_id: UUID
    endpoint_id: UUID
    expected_task_version: int = Field(ge=1)
    expected_endpoint_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    expires_in_seconds: int = Field(default=86400, ge=60, le=604800)

    @field_validator("note")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("A dispatch note is required")
        return value.strip()


class AgentDispatchDecision(Input):
    expected_version: int = Field(ge=1)
    action: Literal["receive", "accept", "decline", "cancel"]
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def reason_required(self):
        if self.action in {"decline", "cancel"} and not self.reason.strip():
            raise ValueError("A reason is required")
        return self


class LeaseInput(Input):
    run_id: UUID
    lease_epoch: int = Field(ge=1)


class ProjectRule(Input):
    id: str = Field(pattern=r"^P-[A-Z0-9-]{1,40}$")
    text: str = Field(min_length=1, max_length=800)


class PolicyInput(Input):
    expected_version: int = Field(ge=0)
    protected_paths: list[str] = Field(default_factory=list, max_length=40)
    forbidden_features: list[str] = Field(default_factory=list, max_length=20)
    rules: list[ProjectRule] = Field(default_factory=list, max_length=20)
    change_reason: str = Field(min_length=10, max_length=1000)

    @field_validator("protected_paths")
    @classmethod
    def safe_paths(cls, values):
        return sorted({document_path(v) for v in values})

    @field_validator("forbidden_features")
    @classmethod
    def features(cls, values):
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError("Each feature must contain 1 to 100 characters")
        return sorted(set(values))

    @field_validator("rules")
    @classmethod
    def unique_rules(cls, values):
        if len({r.id for r in values}) != len(values) or any(not r.text.strip() for r in values):
            raise ValueError("Rule IDs must be unique and text must not be blank")
        return values


class ContextAcknowledgment(LeaseInput):
    context_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rule_ids: list[str] = Field(max_length=40)


class ChangedFile(Input):
    path: str = Field(min_length=1, max_length=800)
    status: Literal["added", "modified", "deleted"]
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0, le=4194304)
    mode: Literal["100644", "100755", "000000"]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return document_path(value)

    @model_validator(mode="after")
    def deleted_content(self):
        if self.status == "deleted":
            if self.sha256 or self.bytes or self.mode != "000000":
                raise ValueError("Deleted entries cannot have content")
        elif not self.sha256 or self.mode == "000000":
            raise ValueError("File content hash and mode required")
        return self


class ChangeEvidence(Input):
    schema_version: Literal[1] = 1
    source: Literal["desktop-git-observer"] = "desktop-git-observer"
    base_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    head_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    files: list[ChangedFile] = Field(max_length=100)

    @field_validator("files")
    @classmethod
    def unique_paths(cls, values):
        if len({f.path.lower() for f in values}) != len(values):
            raise ValueError("Duplicate paths")
        if sum(f.bytes for f in values) > 16777216:
            raise ValueError("Snapshot exceeds 16 MiB")
        return sorted(values, key=lambda f: f.path)


class WorklogInput(LeaseInput):
    turn_id: UUID | None = None
    summary: str = Field(min_length=1, max_length=300)
    notes: str = Field(min_length=1, max_length=16000)
    changed_paths: list[str] = Field(default_factory=list, max_length=100)
    tests: str = Field(min_length=1, max_length=8000)
    next_steps: str = Field(min_length=1, max_length=4000)
    outcome: Literal["checkpoint", "blocked", "failed"] = "checkpoint"
    git_commit: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")
    context_id: UUID | None = None
    change_evidence: ChangeEvidence | None = None

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
    evidence_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


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


class RepositoryInput(Input):
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,79}$")
    name: str = Field(min_length=1, max_length=200)
    platform: Literal["web", "pc", "api", "mobile", "shared", "other"]
    repository_url: str = Field(max_length=400)
    branch: str = Field(default="main", min_length=1, max_length=200)
    auth_mode: Literal["public", "token", "github_app"] = "public"
    token: SecretStr | None = None
    installation_id: int | None = Field(default=None, ge=1)

    @field_validator("repository_url")
    @classmethod
    def repository(cls, value):
        return DocumentRepositoryInput.github_repository(value)

    @field_validator("branch")
    @classmethod
    def branch_name(cls, value):
        return DocumentRepositoryInput.git_branch(value)

    @field_validator("token")
    @classmethod
    def credential(cls, value):
        return DocumentRepositoryInput.bounded_token(value)

    @field_validator("slug")
    @classmethod
    def portable_slug(cls, value):
        if value in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}:
            raise ValueError("Use a portable repository name")
        return value

    @model_validator(mode="after")
    def credential_mode(self):
        if self.auth_mode == "token" and self.token is None:
            raise ValueError("A repository credential is required")
        if self.auth_mode != "token" and self.token is not None:
            raise ValueError("Credential does not match auth_mode")
        if (self.auth_mode == "github_app") != (self.installation_id is not None):
            raise ValueError("GitHub App mode requires an installation ID")
        return self


class RepositoryStatus(Input):
    expected_version: int = Field(ge=1)
    status: Literal["active", "archived"]


class RepositoryCredential(Input):
    expected_version: int = Field(ge=1)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def credential(cls, value):
        return DocumentRepositoryInput.bounded_token(value)


class GitHubAppInput(Input):
    expected_version: int = Field(ge=0)
    app_id: int = Field(ge=1)
    private_key: SecretStr | None = None


class GitTicketInput(Input):
    action: Literal["read", "write"] = "read"
    task_id: UUID | None = None
    run_id: UUID | None = None
    lease_epoch: int | None = Field(default=None, ge=1)
    expected_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")

    @model_validator(mode="after")
    def write_binding(self):
        values = [self.task_id, self.run_id, self.lease_epoch, self.expected_commit]
        if self.action == "write" and not all(values) or self.action == "read" and any(values):
            raise ValueError("Write tickets require a live task run and exact commit")
        return self


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


class MemorySearch(Input):
    query: str = Field(default="", max_length=200)
    search_mode: Literal["hybrid", "keyword"] = "hybrid"
    repository_id: UUID | None = None
    include_history: bool = False
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=12, ge=1, le=50)


class ExperienceVersion(Input):
    kind: Literal["knowledge", "card"]
    id: UUID
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperienceRecall(LeaseInput):
    memory_session_id: UUID
    consumed_units: int = Field(default=0, ge=0, le=24000)
    query: str = Field(default="", max_length=200)
    search_mode: Literal["hybrid", "keyword"] = "hybrid"
    experience_ids: list[UUID] = Field(default_factory=list, max_length=12)
    known: list[ExperienceVersion] = Field(default_factory=list, max_length=32)
    max_tokens: int = Field(default=2048, ge=1024, le=4096)
    limit: int = Field(default=6, ge=1, le=12)
    validate_only: bool = False

    @model_validator(mode="after")
    def distinct_references(self):
        if len(set(self.experience_ids)) != len(self.experience_ids) or len({(p.kind, p.id) for p in self.known}) != len(self.known):
            raise ValueError("Duplicate experience references")
        return self


class MemoryDecision(Input):
    expected_version: int = Field(ge=0)
    state: Literal["active", "conflicted", "retired"]
    reason: str = Field(min_length=1, max_length=2000)


class KnowledgeGeneration(Input):
    method: Literal["local-agent", "human", "server-model"] = "local-agent"
    harness: Harness | None = None
    model_reported: str = Field(default="", max_length=120)
    prompt_version: str = Field(default="knowledge-v1", min_length=1, max_length=80)
    input_tokens_reported: int | None = Field(default=None, ge=0, le=10000000)
    output_tokens_reported: int | None = Field(default=None, ge=0, le=10000000)


class KnowledgeCitation(Input):
    source_card_id: UUID
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_quote: str = Field(min_length=8, max_length=1200)


class KnowledgeInput(Input):
    source_card_id: UUID
    repository_id: UUID | None = None
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_quote: str = Field(min_length=8, max_length=1200)
    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]{0,119}$")
    kind: Literal["constraint", "decision", "interface", "lesson", "pitfall"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=10, max_length=2000)
    reason: str = Field(min_length=10, max_length=1000)
    generation: KnowledgeGeneration = Field(default_factory=KnowledgeGeneration)
    additional_sources: list[KnowledgeCitation] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def bounded_knowledge(self):
        ids = [self.source_card_id, *(source.source_card_id for source in self.additional_sources)]
        if len(set(ids)) != len(ids):
            raise ValueError("Citations must use distinct sources")
        if any(not source.source_quote.strip() or len(source.source_quote.encode()) > 4000
               for source in self.additional_sources):
            raise ValueError("A bounded exact quote is required for each citation")
        if any(not value.strip() for value in (self.title, self.body, self.source_quote, self.reason)):
            raise ValueError("Knowledge content must not be blank")
        if len(self.body.encode()) > 6000 or len(self.source_quote.encode()) > 4000:
            raise ValueError("Knowledge content exceeds its UTF-8 byte budget")
        return self


class KnowledgeDecision(Input):
    repository_id: UUID | None = None
    expected_version: int = Field(ge=1)
    state: Literal["active", "conflicted", "retired", "rejected"]
    reason: str = Field(min_length=10, max_length=2000)
    override_reason: str = Field(default="", max_length=2000)
    replaces_id: UUID | None = None
    replaces_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def replacement(self):
        if (self.replaces_id is None) != (self.replaces_version is None):
            raise ValueError("Replacement requires both the fact ID and its current version")
        if self.replaces_id and self.state != "active":
            raise ValueError("Only activation may replace an active fact")
        if not self.reason.strip() or self.override_reason and len(self.override_reason.strip()) < 10:
            raise ValueError("A meaningful reason is required")
        return self
