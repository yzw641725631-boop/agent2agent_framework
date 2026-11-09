"""Read-only provider observations. No merge, deployment, arbitrary URLs or remote commands."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Depends, Query, Request
from pydantic import Field, model_validator
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import github_credentials, repositories, tasks
from . import schemas as S
from .models import Base, CodeDelivery, Idempotency, Identity, ProjectProgress
from .security import HubError, now, rate_limit
from .work_policy import digest

KINDS = Literal["pull_request", "ci", "merge", "deployment", "rollback"]
PERMISSIONS = {
    "pull_request": "pull_requests",
    "merge": "pull_requests",
    "ci": "actions",
    "deployment": "deployments",
    "rollback": "deployments",
}


class DeliveryPolicy(Base):
    __tablename__ = "delivery_policies"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_repositories.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class DeliveryEvidence(Identity, Base):
    __tablename__ = "delivery_evidence"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_repositories.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    worklog_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("worklogs.id"))
    delivery_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("code_deliveries.id"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    provider: Mapped[str] = mapped_column(String(24), default="github")
    kind: Mapped[str] = mapped_column(String(24))
    provider_id: Mapped[int] = mapped_column(BigInteger)
    git_commit: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    repository_version: Mapped[int] = mapped_column(Integer)
    successful: Mapped[bool]
    observation: Mapped[dict] = mapped_column(JSONB)
    rollback_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_evidence.id"))
    rollback_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_evidence.id"))
    merge_evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_evidence.id"))
    receipt_hash: Mapped[str] = mapped_column(String(64))


class PolicyInput(S.Input):
    expected_version: int = Field(ge=0)
    required_kinds: list[KINDS] = Field(default_factory=list, max_length=5)
    workflows: list[str] = Field(default_factory=list, max_length=20)
    environments: list[str] = Field(default_factory=list, max_length=20)
    max_age_seconds: int = Field(default=3600, ge=60, le=86400)
    ci_target: Literal["delivered", "merged"] = "delivered"
    deployment_target: Literal["delivered", "merged"] = "delivered"
    change_reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        if any(len(v) != len(set(v)) for v in [self.required_kinds, self.workflows, self.environments]):
            raise ValueError("Duplicate delivery policy entry")
        if any(not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", p) for p in self.workflows):
            raise ValueError("Use exact repository workflow paths")
        if any(not e.strip() or len(e) > 100 or re.search(r"[\x00-\x1f]", e) for e in self.environments):
            raise ValueError("Invalid environment")
        if "ci" in self.required_kinds and not self.workflows:
            raise ValueError("Required CI needs approved workflow paths")
        if set(self.required_kinds) & {"deployment", "rollback"} and not self.environments:
            raise ValueError("Required deployment needs approved environment names")
        if "merged" in {self.ci_target, self.deployment_target} and "merge" not in self.required_kinds:
            raise ValueError("Merged commit verification requires a mandatory merge receipt")
        if "rollback" in self.required_kinds and "deployment" in self.required_kinds:
            raise ValueError("Choose deployment or rollback for this release")
        return self


class Verify(S.Input):
    worklog_id: uuid.UUID
    expected_task_version: int = Field(ge=1)
    kind: KINDS
    provider_id: int = Field(strict=True, ge=1, le=9007199254740991)
    rollback_of_id: uuid.UUID | None = None
    rollback_to_id: uuid.UUID | None = None
    merge_evidence_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def rollback_reference(self):
        if (self.kind == "rollback") != (self.rollback_of_id is not None) or (self.kind == "rollback") != (
            self.rollback_to_id is not None
        ):
            raise ValueError("Rollback requires both the replaced and destination deployment receipts")
        if self.merge_evidence_id and self.kind not in {"ci", "deployment", "rollback"}:
            raise ValueError("Only CI and deployment may reference a verified merge commit")
        return self


def policy(db, project_id, repository_id):
    row = db.scalar(
        select(DeliveryPolicy)
        .where(DeliveryPolicy.project_id == project_id, DeliveryPolicy.repository_id == repository_id)
        .order_by(DeliveryPolicy.version.desc())
        .limit(1)
    )
    payload = {
        "version": row.version if row else 0,
        **(
            row.content
            if row
            else {
                "required_kinds": [],
                "workflows": [],
                "environments": [],
                "max_age_seconds": 3600,
                "ci_target": "delivered",
                "deployment_target": "delivered",
            }
        ),
    }
    return {**payload, "content_hash": digest(payload)}


def configure(hub, db, identity, project_id, repository_id, body, request_id):
    hub.project(db, identity, project_id, "tasks:publish")
    repositories.require(db, project_id, repository_id)
    previous = policy(db, project_id, repository_id)
    if body.expected_version != previous["version"]:
        raise HubError("DELIVERY_POLICY_VERSION_CONFLICT", 409)
    content = body.model_dump(mode="json", exclude={"expected_version", "change_reason"})
    db.add(
        DeliveryPolicy(
            project_id=project_id,
            repository_id=repository_id,
            version=previous["version"] + 1,
            author_id=identity.user.id,
            device_id=identity.device.id,
            content=content,
            content_hash=digest(content),
        )
    )
    db.flush()
    hub.audit(
        db,
        identity.user.id,
        "delivery.policy_configured",
        repository_id,
        request_id,
        project_id,
        repository_id=str(repository_id),
        reason=body.change_reason,
    )
    return policy(db, project_id, repository_id)


def scope(hub, db, identity, project_id, task_id, body):
    hub.project(db, identity, project_id)
    task = tasks.get_task(db, project_id, task_id)
    tasks.check_version(task, body.expected_task_version)
    if not task.repository_id:
        raise HubError("TASK_REPOSITORY_REQUIRED", 422)
    repository = repositories.require(db, project_id, task.repository_id)
    source = tasks.read_log(db, project_id, task_id, body.worklog_id)
    delivered = db.scalar(
        select(CodeDelivery).where(
            CodeDelivery.project_id == project_id,
            CodeDelivery.task_id == task_id,
            CodeDelivery.repository_id == repository.id,
            CodeDelivery.worklog_id == body.worklog_id,
        )
    )
    if not delivered or not source["delivery"]:
        raise HubError("DELIVERY_CHECKPOINT_NOT_PUSHED", 409)
    return task, repository, delivered


def references(db, project_id, task, repository, delivered, body, config):
    """Every referenced receipt must still be visible, linked and current where applicable."""
    target_commit = delivered.git_commit
    target = config["ci_target" if body.kind == "ci" else "deployment_target"]
    if body.kind in {"ci", "deployment", "rollback"}:
        if (target == "merged") != bool(body.merge_evidence_id):
            raise HubError("DELIVERY_MERGE_EVIDENCE_REQUIRED", 409)
        if body.merge_evidence_id:
            merge = db.get(DeliveryEvidence, body.merge_evidence_id)
            if (
                not merge
                or merge.project_id != project_id
                or merge.repository_id != repository.id
                or merge.task_id != task.id
                or merge.worklog_id != delivered.worklog_id
                or merge.delivery_id != delivered.id
                or merge.kind != "merge"
                or not merge.successful
                or merge.policy_version != config["version"]
                or merge.repository_version != repository.version
                or stamp(merge.observation["observed_at"])
                < now() - timedelta(seconds=config["max_age_seconds"])
            ):
                raise HubError("DELIVERY_MERGE_EVIDENCE_REQUIRED", 409)
            target_commit = sha(merge.observation["merge_commit"])
    previous = destination = None
    if body.kind == "rollback":
        previous = db.get(DeliveryEvidence, body.rollback_of_id)
        destination = db.get(DeliveryEvidence, body.rollback_to_id)
        for row in [previous, destination]:
            if (
                not row
                or row.project_id != project_id
                or row.repository_id != repository.id
                or row.kind not in {"deployment", "rollback"}
                or not row.successful
            ):
                raise HubError("DELIVERY_ROLLBACK_SOURCE_MISMATCH", 409)
        if (
            previous.observation["verified_commit"] == target_commit
            or destination.observation["verified_commit"] != target_commit
            or destination.observation["environment"] != previous.observation["environment"]
            or stamp(destination.observation["deployment_created_at"])
            >= stamp(previous.observation["deployment_created_at"])
        ):
            raise HubError("DELIVERY_ROLLBACK_SOURCE_MISMATCH", 409)
    return target_commit, previous


def number(value):
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    return value


def stamp(value):
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        parsed.tzinfo is None
        or parsed < datetime(2000, 1, 1, tzinfo=UTC)
        or parsed > now() + timedelta(minutes=5)
    ):
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    return parsed


def enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    return value


def same_repository(value, full_name):
    if not isinstance(value, dict) or not isinstance(value.get("full_name"), str):
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    if value["full_name"].casefold() != full_name.casefold():
        raise HubError("DELIVERY_PROVIDER_COMMIT_MISMATCH", 409)


def latest(values):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
    return max(values, key=lambda v: (stamp(v["created_at"]), number(v["id"])))


def load_json(client, path):
    with client.stream("GET", "https://api.github.com" + path) as response:
        if response.status_code != 200:
            raise HubError("DELIVERY_PROVIDER_UNAVAILABLE", 502)
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise HubError("DELIVERY_PROVIDER_INVALID", 502)
            chunks.append(chunk)
        return json.loads(b"".join(chunks))


def provider_observation(repository_url, branch, git_commit, git_ref, body, config, token, *, transport=None):
    """Only fixed GitHub API paths derived from the registered repository and numeric IDs."""
    try:
        started_at = now().isoformat()
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_url):
            raise HubError("DELIVERY_PROVIDER_INVALID", 502)
        full_name = repository_url.removeprefix("https://github.com/")
        base = "/repos/" + full_name
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with httpx.Client(
            transport=transport, timeout=15, trust_env=False, follow_redirects=False, headers=headers
        ) as client:
            if body.kind in {"pull_request", "merge"}:
                value = load_json(client, f"{base}/pulls/{body.provider_id}")
                if (
                    number(value["number"]) != body.provider_id
                    or sha(value["head"]["sha"]) != git_commit
                    or value["head"]["ref"] != git_ref.removeprefix("refs/heads/")
                    or value["base"]["ref"] != branch
                ):
                    raise HubError("DELIVERY_PROVIDER_COMMIT_MISMATCH", 409)
                same_repository(value["head"]["repo"], full_name)
                same_repository(value["base"]["repo"], full_name)
                state = enum(value["state"], {"open", "closed"})
                if type(value["draft"]) is not bool or type(value["merged"]) is not bool:
                    raise HubError("DELIVERY_PROVIDER_INVALID", 502)
                merged = value["merged"]
                if merged:
                    stamp(value["merged_at"])
                    sha(value["merge_commit_sha"])
                    if state != "closed":
                        raise HubError("DELIVERY_PROVIDER_INVALID", 502)
                return {
                    "state": "merged" if merged else "draft" if value["draft"] else state,
                    "successful": merged
                    if body.kind == "merge"
                    else merged or state == "open" and not value["draft"],
                    "merge_commit": value["merge_commit_sha"] if merged else None,
                    "base_branch": branch,
                    "verified_commit": git_commit,
                    "observed_at": started_at,
                    "provider_reports_only": True,
                }
            if body.kind == "ci":
                run_path = f"{base}/actions/runs/{body.provider_id}"
                first = load_json(client, run_path)
                workflow_id = number(first["workflow_id"])
                query = urlencode({"head_sha": git_commit, "per_page": 100})
                page = load_json(client, f"{base}/actions/workflows/{workflow_id}/runs?{query}")
                newest = latest(page["workflow_runs"])
                if number(newest["id"]) != body.provider_id:
                    raise HubError("DELIVERY_PROVIDER_NOT_LATEST", 409)
                # Read after the latest-run check to observe a rerun's current attempt/state.
                value = load_json(client, run_path)
                if (
                    number(value["id"]) != body.provider_id
                    or number(value["workflow_id"]) != workflow_id
                    or sha(value["head_sha"]) != git_commit
                    or sha(newest["head_sha"]) != git_commit
                ):
                    raise HubError("DELIVERY_PROVIDER_COMMIT_MISMATCH", 409)
                same_repository(value["repository"], full_name)
                same_repository(value["head_repository"], full_name)
                if value["path"] not in config["workflows"]:
                    raise HubError("DELIVERY_WORKFLOW_NOT_APPROVED", 409)
                state = enum(
                    value["status"], {"requested", "queued", "pending", "waiting", "in_progress", "completed"}
                )
                conclusion = value.get("conclusion")
                if conclusion is not None:
                    enum(
                        conclusion,
                        {
                            "action_required",
                            "cancelled",
                            "failure",
                            "neutral",
                            "skipped",
                            "stale",
                            "success",
                            "timed_out",
                            "startup_failure",
                        },
                    )
                return {
                    "state": state,
                    "conclusion": conclusion,
                    "workflow": value["path"],
                    "run_attempt": number(value["run_attempt"]),
                    "provider_created_at": stamp(value["created_at"]).isoformat(),
                    "provider_updated_at": stamp(value["updated_at"]).isoformat(),
                    "successful": state == "completed" and conclusion == "success",
                    "verified_commit": git_commit,
                    "observed_at": started_at,
                    "provider_reports_only": True,
                }
            value = load_json(client, f"{base}/deployments/{body.provider_id}")
            if number(value["id"]) != body.provider_id or sha(value["sha"]) != git_commit:
                raise HubError("DELIVERY_PROVIDER_COMMIT_MISMATCH", 409)
            if value["environment"] not in config["environments"]:
                raise HubError("DELIVERY_ENVIRONMENT_NOT_APPROVED", 409)
            query = urlencode({"environment": value["environment"], "per_page": 100})
            newest = latest(load_json(client, f"{base}/deployments?{query}"))
            if number(newest["id"]) != body.provider_id:
                raise HubError("DELIVERY_PROVIDER_NOT_LATEST", 409)
            deployment_time = stamp(value["created_at"])
            statuses = load_json(client, f"{base}/deployments/{body.provider_id}/statuses?per_page=100")
            if not isinstance(statuses, list) or len(statuses) > 100:
                raise HubError("DELIVERY_PROVIDER_INVALID", 502)
            status = latest(statuses) if statuses else None
            if status:
                timestamp = stamp(status["created_at"])
                if (
                    timestamp < deployment_time
                    or status.get("environment", value["environment"]) != value["environment"]
                ):
                    raise HubError("DELIVERY_PROVIDER_INVALID", 502)
                enum(
                    status["state"],
                    {"error", "failure", "inactive", "in_progress", "queued", "pending", "success"},
                )
            return {
                "environment": value["environment"],
                "state": status["state"] if status else "no-status",
                "status_id": number(status["id"]) if status else None,
                "deployment_created_at": deployment_time.isoformat(),
                "status_created_at": stamp(status["created_at"]).isoformat() if status else None,
                "successful": bool(status and status["state"] == "success"),
                "verified_commit": git_commit,
                "observed_at": started_at,
                "provider_reports_only": True,
                "target_health_verified": False,
            }
    except httpx.RequestError:
        raise HubError("DELIVERY_PROVIDER_UNAVAILABLE", 502) from None
    except HubError:
        raise
    except Exception:
        raise HubError("DELIVERY_PROVIDER_INVALID", 502) from None


def view(row):
    return {
        "id": str(row.id),
        "kind": row.kind,
        "provider": row.provider,
        "provider_id": row.provider_id,
        "worklog_id": str(row.worklog_id),
        "git_commit": row.git_commit,
        "policy_version": row.policy_version,
        "repository_version": row.repository_version,
        "successful": row.successful,
        "observation": row.observation,
        "receipt_hash": row.receipt_hash,
        "created_at": row.created_at.isoformat(),
        "rollback_of_id": str(row.rollback_of_id) if row.rollback_of_id else None,
        "rollback_to_id": str(row.rollback_to_id) if row.rollback_to_id else None,
        "merge_evidence_id": str(row.merge_evidence_id) if row.merge_evidence_id else None,
    }


def current_receipt(result, config, repository):
    if result["policy_version"] != config["version"] or result["repository_version"] != repository.version:
        raise HubError("DELIVERY_VERIFICATION_STALE", 409)
    return result


def save(hub, db, identity, project_id, task_id, body, observation, expected, request_id):
    task, repository, delivered = scope(hub, db, identity, project_id, task_id, body)
    config = policy(db, project_id, repository.id)
    if (repository.version, str(delivered.id), config["content_hash"]) != expected:
        raise HubError("DELIVERY_VERIFICATION_STALE", 409)
    target_commit, previous = references(db, project_id, task, repository, delivered, body, config)
    if target_commit != observation["verified_commit"]:
        raise HubError("DELIVERY_VERIFICATION_STALE", 409)
    if previous and (
        previous.observation["environment"] != observation["environment"]
        or stamp(previous.observation["deployment_created_at"]) >= stamp(observation["deployment_created_at"])
    ):
        raise HubError("DELIVERY_ROLLBACK_SOURCE_MISMATCH", 409)
    payload = {
        "project_id": str(project_id),
        "repository_id": str(repository.id),
        "task_id": str(task_id),
        "worklog_id": str(body.worklog_id),
        "delivery_id": str(delivered.id),
        "author_id": str(identity.user.id),
        "device_id": str(identity.device.id),
        "provider": "github",
        "kind": body.kind,
        "provider_id": body.provider_id,
        "git_commit": delivered.git_commit,
        "policy_version": config["version"],
        "repository_version": repository.version,
        "observation": observation,
        "successful": observation["successful"],
        "rollback_of_id": str(body.rollback_of_id) if body.rollback_of_id else None,
        "rollback_to_id": str(body.rollback_to_id) if body.rollback_to_id else None,
        "merge_evidence_id": str(body.merge_evidence_id) if body.merge_evidence_id else None,
    }
    row = DeliveryEvidence(
        **{
            **payload,
            "project_id": project_id,
            "repository_id": repository.id,
            "task_id": task_id,
            "worklog_id": body.worklog_id,
            "delivery_id": delivered.id,
            "author_id": identity.user.id,
            "device_id": identity.device.id,
            "rollback_of_id": body.rollback_of_id,
            "rollback_to_id": body.rollback_to_id,
            "merge_evidence_id": body.merge_evidence_id,
        },
        receipt_hash=digest(payload),
    )
    db.add(row)
    db.flush()
    hub.audit(
        db,
        identity.user.id,
        "delivery.observed",
        row.id,
        request_id,
        project_id,
        repository_id=str(repository.id),
        kind=body.kind,
        successful=row.successful,
        git_commit=delivered.git_commit,
    )
    db.add(
        ProjectProgress(
            project_id=project_id,
            repository_id=repository.id,
            author_id=identity.user.id,
            device_id=identity.device.id,
            status="checkpoint",
            summary=(
                "交付核验 · "
                + {
                    "pull_request": "合并请求",
                    "ci": "自动测试",
                    "merge": "合并",
                    "deployment": "部署",
                    "rollback": "回滚",
                }[body.kind]
                + " · "
                + task.title
            )[:300],
            detail="提供方已报告成功" if row.successful else "提供方尚未报告成功",
            task_reference=str(task.id),
            task_id=task.id,
            kind="delivery.observed",
            git_commit=delivered.git_commit,
        )
    )
    return view(row)


def release_status(db, task):
    if not task.repository_id:
        return {"applicable": False, "ready": True, "checks": [], "policy_version": 0}
    repository = repositories.require(db, task.project_id, task.repository_id)
    config = policy(db, task.project_id, task.repository_id)
    workflow = DeliveryEvidence.observation["workflow"].astext
    environment = DeliveryEvidence.observation["environment"].astext
    observed = DeliveryEvidence.observation["observed_at"].astext
    rows = list(
        db.scalars(
            select(DeliveryEvidence)
            .distinct(DeliveryEvidence.kind, workflow, environment)
            .where(
                DeliveryEvidence.project_id == task.project_id,
                DeliveryEvidence.task_id == task.id,
                DeliveryEvidence.worklog_id == task.submission_id,
                DeliveryEvidence.policy_version == config["version"],
                DeliveryEvidence.repository_version == repository.version,
            )
            .order_by(
                DeliveryEvidence.kind,
                workflow,
                environment,
                observed.desc(),
                DeliveryEvidence.created_at.desc(),
                DeliveryEvidence.id,
            )
        )
    )
    current = {
        (r.kind, r.observation.get("workflow") or r.observation.get("environment") or ""): r for r in rows
    }
    cutoff = now() - timedelta(seconds=config["max_age_seconds"])
    merge = current.get(("merge", ""))
    checks = []
    delivered = db.scalar(select(CodeDelivery).where(CodeDelivery.worklog_id == task.submission_id))
    for kind in config["required_kinds"]:
        subjects = (
            config["workflows"]
            if kind == "ci"
            else config["environments"]
            if kind in {"deployment", "rollback"}
            else [""]
        )
        for subject in subjects:
            row = current.get((kind, subject))
            target = config["ci_target" if kind == "ci" else "deployment_target"]
            needs_merge = kind in {"ci", "deployment", "rollback"} and target == "merged"
            expected_commit = (
                merge.observation.get("merge_commit")
                if needs_merge and merge
                else delivered.git_commit
                if delivered and not needs_merge
                else None
            )
            fresh = bool(row and stamp(row.observation["observed_at"]) >= cutoff)
            passed = bool(
                row
                and row.successful
                and fresh
                and expected_commit
                and row.observation["verified_commit"] == expected_commit
            )
            if needs_merge:
                passed = bool(
                    passed
                    and merge
                    and merge.successful
                    and stamp(merge.observation["observed_at"]) >= cutoff
                )
            checks.append(
                {
                    "kind": kind,
                    "subject": subject,
                    "passed": passed,
                    "fresh": fresh,
                    "evidence_id": str(row.id) if row else None,
                    "observed_at": row.observation["observed_at"] if row else None,
                    "state": row.observation["state"] if row else "missing",
                }
            )
    return {
        "applicable": True,
        "submitted": bool(task.submission_id),
        "worklog_id": str(task.submission_id) if task.submission_id else None,
        "ready": bool(task.submission_id) and all(c["passed"] for c in checks),
        "checks": checks,
        "policy_version": config["version"],
        "repository_version": repository.version,
        "max_age_seconds": config["max_age_seconds"],
        "verification": "last_read_only_provider_observation",
    }


def check_release(db, task):
    if not release_status(db, task)["ready"]:
        raise HubError("DELIVERY_EXTERNAL_EVIDENCE_REQUIRED", 409)


def install(app, hub, actor, token, mutate, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]

    @app.get("/api/projects/{project_id}/repositories/{repository_id}/delivery-policy")
    def read_policy(project_id: uuid.UUID, repository_id: uuid.UUID, request: Request, db: DB):
        hub.project(db, actor(request, db), project_id)
        repositories.require(db, project_id, repository_id)
        return policy(db, project_id, repository_id)

    @app.put("/api/projects/{project_id}/repositories/{repository_id}/delivery-policy")
    def configure_policy(
        project_id: uuid.UUID, repository_id: uuid.UUID, body: PolicyInput, request: Request, db: DB
    ):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        repositories.require(db, project_id, repository_id)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: configure(hub, db, identity, project_id, repository_id, body, request.state.request_id),
        )

    @app.get("/api/projects/{project_id}/tasks/{task_id}/delivery-evidence")
    def page(
        project_id: uuid.UUID,
        task_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(30, ge=1, le=100),
        repository_id: uuid.UUID | None = None,
    ):
        hub.project(db, actor(request, db), project_id)
        task = tasks.get_task(db, project_id, task_id)
        if repository_id and task.repository_id != repository_id:
            raise HubError("TASK_REPOSITORY_MISMATCH", 404)
        if task.repository_id:
            repositories.require(db, project_id, task.repository_id)
        rows = list(
            db.scalars(
                select(DeliveryEvidence)
                .where(DeliveryEvidence.project_id == project_id, DeliveryEvidence.task_id == task_id)
                .order_by(DeliveryEvidence.created_at.desc(), DeliveryEvidence.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return {
            "items": [view(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_offset": offset + min(len(rows), limit),
            "release": release_status(db, task),
            "task_version": task.version,
            "can_verify": bool(
                db.scalar(select(func.hub.has_project_cap(project_id, "tasks:write")))
                or db.scalar(select(func.hub.has_project_cap(project_id, "tasks:publish")))
            ),
        }

    @app.post("/api/projects/{project_id}/tasks/{task_id}/delivery-evidence")
    def verify(
        project_id: uuid.UUID,
        task_id: uuid.UUID,
        body: Verify,
        request: Request,
        db: DB,
        repository_id: uuid.UUID | None = None,
    ):
        identity = actor(request, db)
        if not db.scalar(select(func.hub.has_project_cap(project_id, "tasks:write"))) and not db.scalar(
            select(func.hub.has_project_cap(project_id, "tasks:publish"))
        ):
            raise HubError("CAPABILITY_REQUIRED", 403)
        rate_limit(security_sessions, hub.secret, f"delivery-verify:{identity.user.id}", 30, 60)
        task, repo, delivered = scope(hub, db, identity, project_id, task_id, body)
        if repository_id and repo.id != repository_id:
            raise HubError("TASK_REPOSITORY_MISMATCH", 404)
        config = policy(db, project_id, repo.id)
        target_commit, _ = references(db, project_id, task, repo, delivered, body, config)
        key = request.headers.get("Idempotency-Key")
        if not key or not 8 <= len(key) <= 128 or not key.isascii():
            raise HubError("IDEMPOTENCY_KEY_REQUIRED", 400)
        cached = db.scalar(
            select(Idempotency).where(
                Idempotency.actor_id == identity.user.id,
                Idempotency.scope == f"{request.method}:{request.url.path}",
                Idempotency.key == key,
            )
        )
        if cached:

            def unavailable():
                raise HubError("DELIVERY_VERIFICATION_STALE", 409)

            return current_receipt(mutate(request, db, identity, body, unavailable), config, repo)
        expected = (repo.version, str(delivered.id), config["content_hash"])
        material = github_credentials.credential_material(
            hub, db, repo, read_permissions=[PERMISSIONS[body.kind]]
        )
        values = (repo.repository_url, repo.branch, target_commit, delivered.git_ref)
        verified = (token(request), identity.device.thumbprint)
        db.rollback()
        db.expunge_all()  # Never hold identity/project locks through provider I/O.
        credential = github_credentials.exchange_credential(
            material, transport=getattr(app.state, "delivery_transport", None)
        )
        observation = provider_observation(
            *values, body, config, credential, transport=getattr(app.state, "delivery_transport", None)
        )
        identity = actor(request, db, verified=verified)
        hub.project(db, identity, project_id)
        if not db.scalar(select(func.hub.has_project_cap(project_id, "tasks:write"))) and not db.scalar(
            select(func.hub.has_project_cap(project_id, "tasks:publish"))
        ):
            raise HubError("CAPABILITY_REQUIRED", 403)
        task, repo, delivered = scope(hub, db, identity, project_id, task_id, body)
        config = policy(db, project_id, repo.id)
        if (repo.version, str(delivered.id), config["content_hash"]) != expected:
            raise HubError("DELIVERY_VERIFICATION_STALE", 409)
        references(db, project_id, task, repo, delivered, body, config)
        result = mutate(
            request,
            db,
            identity,
            body,
            lambda: save(
                hub, db, identity, project_id, task_id, body, observation, expected, request.state.request_id
            ),
        )
        return current_receipt(result, config, repo)
