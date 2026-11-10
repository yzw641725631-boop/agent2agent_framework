"""Central generation worker: reserve, infer without DB locks, reauthorize, then commit.

Repository authorization is issued by the superadmin. The worker never creates user
tokens or grants, executes project instructions, or approves its own results.
"""

import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text

from . import distillation, generation, guides, repositories
from .db import context, coordinate, session_factory
from .generation_provider import Provider
from .insights import require_budget
from .models import Device, Grant, User, Worklog
from .security import HubError, now
from .server_generation import Call, Configuration, RepositoryPolicy, RetryRequest, latest
from .service import Hub, Principal

log = logging.getLogger(__name__)


class GenerationWorker:
    def __init__(self, settings, *, factory=None, provider=None):
        self.settings = settings
        self.sessions = factory or session_factory(settings, security=True)
        self.config = Configuration(settings)
        self.provider = provider or Provider(test=settings.environment == "test")
        self.hub = Hub(settings, None)
        self.scan_offsets = {}
        self.last_repository = None

    def authorize(self, db, policy, user_id=None, device_id=None):
        # The original enabling device must remain trusted; changing policy, disabling
        # its issuer/device or archiving the repository immediately blocks new commits.
        context(db, policy.author_id, policy.device_id)
        if not db.scalar(select(func.hub.is_superadmin())):
            raise HubError("GENERATION_AUTHORIZATION_REVOKED", 403)
        current = latest(db, policy.repository_id)
        if not current or current.version != policy.version or not current.content["enabled"]:
            raise HubError("GENERATION_AUTHORIZATION_CHANGED", 409)
        repositories.require(db, policy.project_id, policy.repository_id, "code:write")
        user_id, device_id = user_id or policy.author_id, device_id or policy.device_id
        user, device = db.get(User, user_id), db.get(Device, device_id)
        if (
            not user
            or not device
            or user.status != "active"
            or device.status != "approved"
            or device.user_id != user.id
            or user.email
            and (not user.email_verified_at or not device.email_verified_at)
        ):
            raise HubError("GENERATION_ACTOR_REVOKED", 403)
        grant = None
        if user.platform_role != "superadmin":
            grants = list(
                db.scalars(
                    select(Grant)
                    .where(
                        Grant.user_id == user.id,
                        Grant.device_id == device.id,
                        Grant.project_id == policy.project_id,
                        Grant.status == "active",
                        Grant.expires_at > now(),
                    )
                    .order_by(Grant.created_at.desc())
                )
            )
            for candidate in grants:
                try:
                    self.hub.validate_grant(db, user, device, candidate)
                    if "tasks:write" in candidate.capabilities and any(
                        p.get("repository_id") == str(policy.repository_id)
                        and "code:write" in p.get("capabilities", [])
                        for p in candidate.repository_permissions
                    ):
                        grant = candidate
                        break
                except HubError:
                    continue
            if not grant:
                raise HubError("GENERATION_ACTOR_REVOKED", 403)
        context(db, user.id, device.id, grant.id if grant else None)
        identity = Principal(user, device, None, grant)
        self.hub.project(db, identity, policy.project_id, "tasks:write")
        repositories.require(db, policy.project_id, policy.repository_id, "code:write")
        return identity

    def guard(self, db):
        if not db.scalar(text("SELECT pg_has_role(current_user,'hub_generation','member')")):
            raise RuntimeError("Generation worker requires the separate hub_generation database role")

    def reserve(self):
        with self.sessions.begin() as db:
            self.guard(db)
            # Serializes the central daily reservation across workers and projects.
            db.execute(text("SELECT pg_advisory_xact_lock(7206111025)"))
            config, version = self.config.read(db)
            if not config:
                return None
            # A killed request has unknown billing. It is never repeated automatically.
            expired = list(
                db.scalars(
                    select(Call).where(Call.state.in_(["reserved", "calling"]), Call.lease_until < now())
                )
            )
            for row in expired:
                row.state, row.error_code, row.finished_at = (
                    "uncertain",
                    "GENERATION_WORKER_INTERRUPTED",
                    now(),
                )
            if expired:
                # Release call-row locks before any identity/project coordination.
                return None
            pending = db.scalar(select(Call).where(Call.state == "ready").order_by(Call.created_at).limit(1))
            if pending:
                return {"resume": pending.id}
            day = now().replace(hour=0, minute=0, second=0, microsecond=0)
            calls, reserved = db.execute(
                select(func.count(Call.id), func.coalesce(func.sum(Call.reserved_tokens), 0)).where(
                    Call.created_at >= day
                )
            ).one()
            if calls >= config["daily_call_limit"]:
                return None
            policies = list(
                db.scalars(
                    select(RepositoryPolicy)
                    .distinct(RepositoryPolicy.repository_id)
                    .order_by(RepositoryPolicy.repository_id, RepositoryPolicy.version.desc())
                )
            )
            if self.last_repository:
                policies.sort(
                    key=lambda p: (str(p.repository_id) <= str(self.last_repository), str(p.repository_id))
                )
            for policy in policies:
                if not policy.content["enabled"]:
                    continue
                try:
                    with db.begin_nested():
                        self.authorize(db, policy)
                        require_budget(db, policy.project_id)
                        kinds = [
                            k
                            for k in ("distillation", "guide")
                            if policy.content.get("guides" if k == "guide" else k)
                        ]
                        for kind in kinds:
                            model = (
                                distillation.DistillationJob if kind == "distillation" else guides.GuideJob
                            )
                            key = (policy.repository_id, kind)
                            candidates = list(
                                db.scalars(
                                    select(model)
                                    .where(
                                        model.repository_id == policy.repository_id,
                                        (model.state == "queued")
                                        | ((model.state == "running") & (model.lease_until < now())),
                                        ~select(Call.id)
                                        .where(
                                            Call.kind == kind,
                                            Call.job_id == model.id,
                                            Call.state != "abandoned",
                                        )
                                        .exists(),
                                    )
                                    .order_by(model.created_at, model.id)
                                    .offset(self.scan_offsets.get(key, 0))
                                    .limit(20)
                                )
                            )
                            if not candidates:
                                self.scan_offsets[key] = 0
                                continue
                            self.scan_offsets[key] = self.scan_offsets.get(key, 0) + len(candidates)
                            for candidate in candidates:
                                try:
                                    with db.begin_nested():
                                        task = self.reserve_job(
                                            db, policy, candidate, kind, config, version, reserved
                                        )
                                        if task:
                                            self.scan_offsets[key] = 0
                                            self.last_repository = policy.repository_id
                                            return task
                                except HubError as error:
                                    log.warning(
                                        "Server generation source deferred: %s (%s)", candidate.id, error.code
                                    )
                except HubError:
                    # A repository with revoked authority or changed sources cannot block others.
                    continue
        return None

    def reserve_job(self, db, policy, candidate, kind, config, provider_version, reserved):
        coordinate(
            db, user_ids=(policy.author_id, candidate.target_user_id), project_ids=(policy.project_id,)
        )
        identity = self.authorize(db, policy)
        model = distillation.DistillationJob if kind == "distillation" else guides.GuideJob
        job = db.scalar(select(model).where(model.id == candidate.id).with_for_update(skip_locked=True))
        if not job:
            return None
        if kind == "distillation":
            instance = policy.instance_id
            bundle = distillation.claim(
                self.hub,
                db,
                identity,
                policy.project_id,
                job,
                distillation.Claim(expected_version=job.version, instance_id=instance),
                str(uuid.uuid4()),
            )
        else:
            identity = self.authorize(db, policy, job.target_user_id, job.target_device_id)
            instance = job.instance_id
            guides.claim(
                self.hub,
                db,
                identity,
                job,
                guides.Claim(expected_version=job.version, instance_id=instance),
                str(uuid.uuid4()),
            )
            bundle = self.guide_input(db, job)
        payload = self.provider.prepare(config, kind, bundle)
        reservation = len(payload) + config["max_output_tokens"]
        if reserved + reservation > config["daily_token_limit"]:
            raise HubError("GENERATION_DAILY_BUDGET", 409)
        if not db.scalar(select(func.hub.generation_project_budget_open(policy.project_id, reservation))):
            raise HubError("GENERATION_PROJECT_BUDGET", 409)
        row = Call(
            project_id=policy.project_id,
            repository_id=policy.repository_id,
            policy_version=policy.version,
            provider_version=provider_version,
            provider_model=config["model"],
            kind=kind,
            job_id=job.id,
            state="reserved",
            lease_token=uuid.uuid4(),
            lease_until=now() + timedelta(seconds=180),
            user_id=identity.user.id,
            device_id=identity.device.id,
            instance_id=instance,
            job_version=job.version,
            job_lease_epoch=job.lease_epoch,
            source_hash=job.source_hash if kind == "guide" else job.fingerprint,
            reserved_tokens=reservation,
            input_bytes=len(payload),
        )
        db.add(row)
        db.flush()
        return {"call_id": row.id, "lease": row.lease_token, "config": config, "payload": payload}

    def apply_retry(self):
        with self.sessions.begin() as db:
            self.guard(db)
            request = db.scalar(
                select(RetryRequest)
                .where(RetryRequest.state == "pending")
                .order_by(RetryRequest.created_at, RetryRequest.id)
                .limit(1)
            )
            if not request:
                return
            initial = db.get(Call, request.call_id)
            policy = latest(db, initial.repository_id)
            coordinate(
                db,
                user_ids=(request.user_id, initial.user_id, policy.author_id if policy else None),
                project_ids=(initial.project_id,),
            )
            request = db.scalar(
                select(RetryRequest)
                .where(RetryRequest.id == request.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if request.state != "pending":
                return
            try:
                with db.begin_nested():
                    context(db, request.user_id, request.device_id)
                    if not db.scalar(select(func.hub.is_superadmin())):
                        raise HubError("GENERATION_RETRY_APPROVER_REVOKED", 403)
                    repositories.require(db, initial.project_id, initial.repository_id, "code:write")
                    row = db.scalar(
                        select(Call)
                        .where(Call.id == initial.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if (
                        row.state not in {"blocked", "uncertain"}
                        or not policy
                        or not policy.content["enabled"]
                    ):
                        raise HubError("GENERATION_CALL_NOT_RETRYABLE", 409)
                    model = distillation.DistillationJob if row.kind == "distillation" else guides.GuideJob
                    job = db.scalar(select(model).where(model.id == row.job_id).with_for_update())
                    if not job:
                        raise HubError("GENERATION_SOURCE_CHANGED", 409)
                    if request.allow_new_provider_call:
                        # The original lease must end before another paid request can start.
                        # Do not modify a live or reassigned developer's task.
                        row.state = "abandoned"
                    else:
                        if (
                            not row.encrypted_result
                            or job.version != row.job_version
                            or job.lease_epoch != row.job_lease_epoch
                        ):
                            raise HubError("GENERATION_SOURCE_CHANGED", 409)
                        identity = self.authorize(db, policy, row.user_id, row.device_id)
                        if (job.source_hash if row.kind == "guide" else job.fingerprint) != row.source_hash:
                            raise HubError("GENERATION_SOURCE_CHANGED", 409)
                        if job.state == "running" and job.lease_until <= now():
                            if row.kind == "distillation":
                                distillation.claim(
                                    self.hub,
                                    db,
                                    identity,
                                    row.project_id,
                                    job,
                                    distillation.Claim(
                                        expected_version=job.version, instance_id=row.instance_id
                                    ),
                                    str(request.id),
                                )
                            else:
                                guides.claim(
                                    self.hub,
                                    db,
                                    identity,
                                    job,
                                    guides.Claim(expected_version=job.version, instance_id=row.instance_id),
                                    str(request.id),
                                )
                            row.job_version, row.job_lease_epoch = job.version, job.lease_epoch
                        row.policy_version, row.state, row.error_code = policy.version, "ready", None
                        self.revalidate(db, row)
                    request.state = "applied"
            except HubError as error:
                request.state, request.error_code = "rejected", error.code

    def guide_input(self, db, job):
        from . import tasks

        source = db.get(Worklog, job.source_worklog_id)
        if not source or source.receipt_hash != job.source_hash:
            raise HubError("GUIDE_SOURCE_CHANGED", 409)
        return {
            "plan": job.plan,
            "task": tasks.task_view(tasks.get_task(db, job.project_id, job.task_id)),
            "worklog": tasks.log_view(source),
            "source_hash": job.source_hash,
        }

    def lock_call(self, db, call_id):
        initial = db.get(Call, call_id)
        if not initial:
            raise HubError("GENERATION_CALL_NOT_ACCESSIBLE", 404)
        policy = db.get(RepositoryPolicy, (initial.repository_id, initial.policy_version))
        coordinate(
            db,
            user_ids=(initial.user_id, policy.author_id if policy else None),
            project_ids=(initial.project_id,),
        )
        return db.scalar(
            select(Call).where(Call.id == call_id).with_for_update().execution_options(populate_existing=True)
        )

    def revalidate(self, db, row, *, before_request=False):
        policy = db.get(RepositoryPolicy, (row.repository_id, row.policy_version))
        if not policy:
            raise HubError("GENERATION_AUTHORIZATION_CHANGED", 409)
        coordinate(db, user_ids=(policy.author_id, row.user_id), project_ids=(row.project_id,))
        identity = self.authorize(db, policy, row.user_id, row.device_id)
        _, provider_version = self.config.read(db)
        if before_request and provider_version != row.provider_version:
            raise HubError("GENERATION_CONFIGURATION_VERSION_CONFLICT", 409)
        if before_request:
            require_budget(db, row.project_id)
            if not db.scalar(select(func.hub.generation_project_budget_open(row.project_id, 0))):
                raise HubError("GENERATION_PROJECT_BUDGET", 409)
        model = distillation.DistillationJob if row.kind == "distillation" else guides.GuideJob
        job = db.get(model, row.job_id)
        if not job:
            raise HubError("GENERATION_SOURCE_CHANGED", 409)
        if row.kind == "distillation":
            distillation.live(
                self.hub,
                db,
                identity,
                row.project_id,
                job,
                distillation.Retry(
                    expected_version=row.job_version,
                    instance_id=row.instance_id,
                    lease_epoch=row.job_lease_epoch,
                    reason="Verify current authorization before the provider request",
                ),
            )
        else:
            guides.live(
                self.hub,
                db,
                identity,
                job,
                guides.Retry(
                    expected_version=row.job_version,
                    instance_id=row.instance_id,
                    lease_epoch=row.job_lease_epoch,
                    reason="Verify frozen generation sources",
                ),
            )
            self.guide_input(db, job)
        return identity, job

    def run_once(self):
        self.apply_retry()
        task = self.reserve()
        if not task:
            return False
        call_id = task.get("resume") or task["call_id"]
        try:
            if "resume" in task:
                self.commit_result(call_id)
                return True
            with self.sessions.begin() as db:
                row = self.lock_call(db, call_id)
                if row.state != "reserved" or row.lease_token != task["lease"] or row.lease_until <= now():
                    raise HubError("GENERATION_CALL_STALE", 409)
                self.revalidate(db, row, before_request=True)
                row.state = "calling"
            # No database connection, advisory lock or user token is held across inference.
            result = self.provider.invoke(task["config"], task["payload"], call_id)
            with self.sessions.begin() as db:
                row = db.get(Call, call_id)
                if row.state != "calling" or row.lease_token != task["lease"] or row.lease_until <= now():
                    raise HubError("GENERATION_CALL_STALE", 409)
                row.encrypted_result = self.config.seal(result)
                row.response_hash = result["response_hash"]
                row.input_tokens, row.output_tokens = result["input_tokens"], result["output_tokens"]
                row.state = "ready"
            self.commit_result(call_id)
        except Exception as error:
            with self.sessions.begin() as db:
                row = db.get(Call, call_id)
                if row and row.state != "shared":
                    row.state = "blocked" if row.encrypted_result else "uncertain"
                    row.error_code = error.code if isinstance(error, HubError) else "GENERATION_FAILED"
                    row.finished_at = now()
            log.warning("Server generation deferred; original sources and billing reservation retained")
        return True

    def commit_result(self, call_id):
        with self.sessions.begin() as db:
            self.guard(db)
            row = self.lock_call(db, call_id)
            if not row or row.state != "ready":
                return
            identity, job = self.revalidate(db, row)
            result = self.config.open(row.encrypted_result)
            report = generation.Report(
                execution="server-background",
                provider="openai-compatible",
                model=row.provider_model,
                response_hash=result["response_hash"],
                input_bytes=row.input_bytes,
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
            )
            lease = {
                "expected_version": row.job_version,
                "instance_id": row.instance_id,
                "lease_epoch": row.job_lease_epoch,
                "source_hash": row.source_hash,
            }
            value = result["value"]
            if row.kind == "distillation":
                if set(value) - {"outcome", "candidates", "reason"}:
                    raise HubError("GENERATION_INVALID_OUTPUT", 422)
                for candidate in value.get("candidates", []):
                    if candidate.get("repository_id") or candidate.get("generation"):
                        raise HubError("GENERATION_INVALID_OUTPUT", 422)
                    candidate["generation"] = {
                        "method": "server-model",
                        "model_reported": report.model,
                        "prompt_version": "server-background-v1",
                        "input_tokens_reported": result["input_tokens"],
                        "output_tokens_reported": result["output_tokens"],
                    }
                distillation.complete(
                    self.hub,
                    db,
                    identity,
                    row.project_id,
                    job,
                    distillation.Complete(**lease, **value),
                    str(row.id),
                    generation_report=report,
                )
            else:
                guides.complete(
                    self.hub,
                    db,
                    identity,
                    job,
                    guides.Complete(**lease, content=guides.GuideContent(**value)),
                    str(row.id),
                    generation_report=report,
                )
            row.state, row.finished_at, row.error_code, row.encrypted_result = "shared", now(), None, None

    def serve(self):
        with self.sessions() as db:
            self.guard(db)
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.error("Server generation transaction deferred; no provider secrets logged")
                worked = False
            if not worked:
                time.sleep(5)
