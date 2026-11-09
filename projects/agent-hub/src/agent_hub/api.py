import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from . import (
    __version__,
    closeouts,
    documents,
    git_gateway,
    github_credentials,
    memory,
    progress,
    repositories,
    roles,
    tasks,
    work_archives,
    work_policy,
)
from . import schemas as S
from .administration import Setup, device_view, email_change_complete, email_change_start, grant_view
from .config import Settings, get_settings
from .db import context, coordinate, session_factory
from .mail import Mailer
from .mail_outbox import MailOutbox
from .mail_outbox import enqueue as enqueue_mail
from .models import Approval, Audit, AuthSession, Device, Enrollment, Grant, Membership, Project, User
from .security import HubError, check_proof, now, rate_limit
from .service import ADMIN_CAPS, Hub, approval_view, require_root, user_view


def create_app(
    settings: Settings | None = None, *, factory=None, security_factory=None, mailer=None
) -> FastAPI:
    cfg = settings or get_settings()
    sessions = factory or session_factory(cfg)
    security_sessions = security_factory or session_factory(cfg, security=True)
    mail = mailer or Mailer(cfg)
    hub = Hub(cfg, mail)
    setup = Setup(hub)
    deliveries = MailOutbox(cfg, sessions, mail)
    from .semantic import Embeddings

    @asynccontextmanager
    async def lifespan(app):
        with sessions() as db:
            role = db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            superuser, bypass = role.one()
            owner = db.scalar(
                text(
                    "SELECT pg_get_userbyid(relowner)=current_user FROM pg_class "
                    "WHERE oid='public.projects'::regclass"
                )
            )
            rls = db.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid='public.projects'::regclass"))
            if superuser or bypass or owner or not rls:
                raise RuntimeError(
                    "API requires a non-owner, non-superuser, non-BYPASSRLS role and migrated RLS"
                )
        stop = asyncio.Event()

        async def recover_mail():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(deliveries.recover_once)
                except Exception:
                    logging.getLogger(__name__).error("Mail outbox recovery deferred")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

        worker = asyncio.create_task(recover_mail()) if cfg.mail_mode != "memory" else None
        try:
            yield
        finally:
            stop.set()
            if worker:
                await worker
            if security_factory is None:
                security_sessions.kw["bind"].dispose()

    app = FastAPI(title="Agent Hub", version=__version__, lifespan=lifespan)
    app.state.sessions, app.state.mailer, app.state.hub = sessions, mail, hub
    app.state.security_sessions, app.state.deliveries = security_sessions, deliveries
    app.state.embeddings = Embeddings(cfg)

    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(HubError)
    async def business_error(request: Request, error: HubError):
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "request_id": request.state.request_id,
                **error.details,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "request_id": request.state.request_id,
                "errors": [
                    {"location": list(e["loc"]), "message": e["msg"], "type": e["type"]}
                    for e in error.errors()
                ],
            },
        )

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(DBAPIError)
    async def database_busy(request: Request, error):
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        if not isinstance(error, PoolTimeout) and code not in {"55P03", "40P01", "57014", "53300"}:
            logging.getLogger(__name__).error("Database request failed")
        return JSONResponse(
            status_code=503,
            content={"code": "SERVICE_BUSY", "request_id": request.state.request_id},
            headers={"Retry-After": "3"},
        )

    def db_dependency(request: Request):
        pending_mail = []
        with sessions() as db:
            db.info["semantic_configured"] = bool(app.state.embeddings.url)
            try:
                yield db
                db.commit()
                pending_mail = db.info.pop("mail_deliveries", [])
            except HubError as exc:
                if exc.persist:
                    db.commit()
                else:
                    db.rollback()
                actor_id = getattr(request.state, "actor_id", None)
                # Unauthenticated admission failures must not become an unbounded audit sink.
                if actor_id or exc.code not in {
                    "RATE_LIMITED",
                    "DEVICE_PROOF_REQUIRED",
                    "DPOP_TOKEN_REQUIRED",
                    "GIT_AUTHENTICATION_REQUIRED",
                }:
                    with security_sessions.begin() as audit_db:
                        hub.audit(
                            audit_db,
                            actor_id,
                            "request.denied",
                            request.url.path[:100],
                            request.state.request_id,
                            code=exc.code,
                        )
                raise
            except Exception:
                db.rollback()
                raise
        for job_id in pending_mail:
            deliveries.deliver(job_id, raise_errors=True)

    DB = Annotated[object, Depends(db_dependency, scope="function")]

    def proof(request, token=None):
        return check_proof(
            security_sessions, cfg, request.headers.get("DPoP", ""), request.method, request.url.path, token
        )

    def token(request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("DPoP ") or not 20 <= len(header[5:]) <= 256:
            raise HubError("DPOP_TOKEN_REQUIRED", 401)
        return header[5:]

    def actor(request, db, *, users=(), projects=(), resources=(), grant_id=None, verified=None):
        value = token(request) if verified is None else verified[0]
        thumb = proof(request, value)[1] if verified is None else verified[1]
        params = request.path_params
        hub.lock_session(
            db,
            value,
            thumb,
            user_ids=[*users, params.get("user_id")],
            project_ids=[*projects, params.get("project_id")],
            device_id=params.get("device_id"),
            approval_id=params.get("approval_id"),
            grant_id=grant_id,
            resources=resources,
            exclusive=request.method not in {"GET", "HEAD"}
            and not request.url.path.endswith(("/documents/read", "/documents/search")),
        )
        identity = hub.authenticate(db, value, thumb)
        request.state.actor_id = identity.user.id
        return identity

    def semantic_query(request, db, identity, query, mode="hybrid"):
        if not app.state.embeddings.url or not query.strip() or mode == "keyword":
            return identity
        value, thumb = token(request), identity.device.thumbprint
        # Preflight authorization is read-only. Release locks before model I/O,
        # then authenticate this already verified request again under fresh locks.
        db.rollback()
        db.expunge_all()
        vector = app.state.embeddings.query(query)
        identity = actor(request, db, verified=(value, thumb))
        db.info["semantic_query"] = vector
        return identity

    def enrollment(request, db, *, grant_id=None):
        value = token(request)
        _, thumb = proof(request, value)
        hub.lock_enrollment(db, value, thumb, grant_id=grant_id)
        user, device = hub.enrollment_identity(db, value, thumb)
        request.state.actor_id = user.id
        return user, device

    def mutate(request, db, identity, body, action):
        # Cached mutation results must never bypass current resource visibility.
        params = request.path_params
        if params.get("task_id"):
            tasks.get_task(db, params["project_id"], params["task_id"])
        if params.get("repository_id") and "/documents" in request.url.path:
            repositories.source_access(hub, db, identity, params["project_id"], params["repository_id"], manage=True)
        return hub.idempotent(
            db,
            identity,
            f"{request.method}:{request.url.path}",
            request.headers.get("Idempotency-Key"),
            body.model_dump(mode="python"),
            action,
        )

    closeouts.install(app, hub, actor, token, proof, db_dependency)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "phase": "P8-reviewed-knowledge", "version": __version__}

    @app.post("/api/auth/enrollments")
    def enroll(body: S.Enroll, request: Request, db: DB):
        rate_limit(
            security_sessions, hub.secret, f"ip:{request.client.host if request.client else 'local'}", 60, 300
        )
        jwk, thumb = proof(request)
        user_id = db.scalar(select(User.id).where(User.login == body.login))
        if user_id:
            rate_limit(security_sessions, hub.secret, f"login:{user_id}", 10, 300)
        coordinate(db, user_ids=[user_id], resources=[f"login:{body.login}"])
        db.expire_all()
        return hub.enroll(db, body, jwk, thumb, request.state.request_id)

    @app.get("/api/devices/enrollment")
    def enrollment_status(request: Request, db: DB):
        user, device = enrollment(request, db)
        hub.confirm_superadmin_device(db, user, device, request.state.request_id)
        return {
            "device_id": str(device.id),
            "status": device.status,
            "email_verification_required": bool(user.email and not device.email_verified_at),
            "platform_role": user.platform_role,
            "device_approval_mode": "root_email" if user.platform_role == "superadmin" else "superadmin",
        }

    @app.post("/api/devices/email-challenges")
    def email_challenge(body: S.Empty, request: Request, db: DB):
        user, device = enrollment(request, db)
        rate_limit(security_sessions, hub.secret, f"mail:{user.id}", 5, 3600)
        return hub.send_email(db, user, device, request.state.request_id)

    @app.post("/api/devices/email-verifications")
    def verify_email(body: S.VerifyEmail, request: Request, db: DB):
        user, device = enrollment(request, db)
        return hub.verify_email(db, user, device, body, request.state.request_id)

    @app.post("/api/auth/sessions")
    def start_session(body: S.StartSession, request: Request, db: DB):
        user, device = enrollment(request, db, grant_id=body.grant_id)
        grant = db.get(Grant, body.grant_id) if body.grant_id else None
        if body.grant_id and (not grant or grant.user_id != user.id or grant.device_id != device.id):
            raise HubError("GRANT_NOT_ACCESSIBLE", 404)
        result = hub.issue_session(db, user, device, grant)
        hub.audit(db, user.id, "session.created", result["session_id"], request.state.request_id)
        return result

    @app.post("/api/auth/refresh")
    def refresh(body: S.Empty, request: Request, db: DB):
        value = token(request)
        _, thumb = proof(request, value)
        hub.lock_session(db, value, thumb, refresh=True)
        identity = hub.authenticate(db, value, thumb, refresh=True)
        identity.session.state = "rotated"
        return hub.issue_session(
            db,
            identity.user,
            identity.device,
            identity.grant,
            family=identity.session.family_id,
            family_expiry=identity.session.refresh_expires_at,
        )

    @app.get("/api/me")
    def me(request: Request, db: DB):
        identity = actor(request, db)
        return {
            "user": user_view(identity.user),
            "device_id": str(identity.device.id),
            "grant_id": str(identity.grant.id) if identity.grant else None,
        }

    @app.get("/api/me/grants")
    def own_grants(request: Request, db: DB):
        identity = actor(request, db)
        grants = db.scalars(
            select(Grant).where(
                Grant.user_id == identity.user.id,
                Grant.device_id == identity.device.id,
                Grant.status == "active",
                Grant.expires_at > now(),
            )
        )
        items = []
        try:
            for g in grants:
                try:
                    hub.validate_grant(db, identity.user, identity.device, g)
                except HubError:
                    continue
                p = db.get(Project, g.project_id)
                items.append({**grant_view(g), "project_name": p.name, "project_slug": p.slug})
        finally:
            context(db, identity.user.id, identity.device.id, identity.session.grant_id)
        return {"items": items}

    @app.post("/api/admin/users/admins")
    def create_admin(body: S.NewUser, request: Request, db: DB):
        resources = [f"login:{body.login}"]
        if body.email:
            resources.append(f"email:{str(body.email).lower()}")
        identity = actor(request, db, resources=resources)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: user_view(hub.create_user(db, identity, body, request.state.request_id, admin=True)),
        )

    @app.post("/api/admin/projects")
    def create_project(body: S.NewProject, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.create_project(db, identity, body, request.state.request_id),
        )

    @app.get("/api/projects")
    def projects(request: Request, db: DB):
        actor(request, db)
        return {
            "items": [
                {"id": str(p.id), "slug": p.slug, "name": p.name}
                for p in db.scalars(select(Project).order_by(Project.slug))
            ]
        }

    @app.get("/api/projects/{project_id}/context")
    def project_context(project_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        p = hub.project(db, identity, project_id)
        return {
            "id": str(p.id),
            "slug": p.slug,
            "name": p.name,
            "repository_mode": p.repository_mode,
            "resource_visibility": repositories.visibility(db, p),
            "authorization": {"grant_id": str(identity.grant.id) if identity.grant else None,
                "user_epoch": identity.user.auth_epoch, "device_epoch": identity.device.epoch},
            "capabilities": sorted(ADMIN_CAPS) if identity.user.platform_role == "superadmin" else identity.grant.capabilities,
        }

    @app.get("/api/projects/{project_id}/repositories")
    def project_repositories(project_id: uuid.UUID, request: Request, db: DB):
        return repositories.page(hub, db, actor(request, db), project_id)

    @app.post("/api/projects/{project_id}/repositories")
    def register_repository(project_id: uuid.UUID, body: S.RepositoryInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.manage_members(db, identity, project_id)
        hub.project(db, identity, project_id, "repositories:manage")
        return mutate(request, db, identity, body,
                      lambda: repositories.register(hub, db, identity, project_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/repositories/{repository_id}/status")
    def repository_status(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.RepositoryStatus, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(request, db, identity, body,
                      lambda: repositories.change_status(hub, db, identity, project_id, repository_id, body, request.state.request_id))

    @app.put("/api/projects/{project_id}/repositories/{repository_id}/credential")
    def repository_credential(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.RepositoryCredential, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(request, db, identity, body,
                      lambda: repositories.change_credential(hub, db, identity, project_id, repository_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/repositories/{repository_id}/git-ticket")
    def repository_git_ticket(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.GitTicketInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        rate_limit(security_sessions, hub.secret, f"git-ticket:{identity.user.id}", 60, 60)
        # Tickets are deliberately not persisted in plaintext idempotency results.
        return git_gateway.issue(hub, db, identity, project_id, repository_id, body, request.state.request_id)

    @app.get("/api/admin/integrations/github")
    def github_app_configuration(request: Request, db: DB):
        require_root(actor(request, db))
        return github_credentials.app_view(hub, db)

    @app.put("/api/admin/integrations/github")
    def configure_github_app(body: S.GitHubAppInput, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(request, db, identity, body,
                      lambda: github_credentials.configure_app(hub, db, identity, body, request.state.request_id))

    @app.put("/api/admin/projects/{project_id}/administrators")
    def assign_admin(project_id: uuid.UUID, body: S.AssignAdmin, request: Request, db: DB):
        identity = actor(request, db, users=[body.user_id])
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.assign_admin(db, identity, project_id, body.user_id, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/member-invitations")
    def invite(project_id: uuid.UUID, body: S.MemberInvitation, request: Request, db: DB):
        resources = [f"login:{body.new_user.login}"] if body.new_user else []
        if body.new_user and body.new_user.email:
            resources.append(f"email:{str(body.new_user.email).lower()}")
        identity = actor(request, db, users=[body.user_id], resources=resources)
        hub.manage_members(db, identity, project_id)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.invite(db, identity, project_id, body, request.state.request_id),
        )

    @app.post("/api/grant-requests")
    def grant_request(body: S.GrantRequest, request: Request, db: DB):
        identity = actor(request, db, users=[body.user_id], projects=[body.project_id])
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.request_grant(db, identity, body, request.state.request_id),
        )

    @app.post("/api/admin/grants")
    def direct_grant(body: S.GrantRequest, request: Request, db: DB):
        identity = actor(request, db, users=[body.user_id], projects=[body.project_id])
        require_root(identity)

        def issue():
            a = hub.request_grant(db, identity, body, request.state.request_id)
            return hub.decide(
                db,
                identity,
                uuid.UUID(a["id"]),
                S.Decision(approve=True, expected_version=a["version"], expected_hash=a["payload_hash"]),
                request.state.request_id,
            )

        return mutate(request, db, identity, body, issue)

    @app.get("/api/admin/approvals")
    def approvals(
        request: Request, db: DB, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)
    ):
        identity = actor(request, db)
        require_root(identity)
        return {
            "items": [
                approval_view(a)
                for a in db.scalars(
                    select(Approval)
                    .where(Approval.status == "pending_approval", Approval.expires_at > now())
                    .order_by(Approval.created_at, Approval.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]
        }

    @app.post("/api/admin/approvals/{approval_id}/decision")
    def decision(approval_id: uuid.UUID, body: S.Decision, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.decide(db, identity, approval_id, body, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/members/{user_id}/revoke")
    def revoke_member(project_id: uuid.UUID, user_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        hub.manage_members(db, identity, project_id)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.revoke_member(db, identity, project_id, user_id, request.state.request_id),
        )

    @app.post("/api/admin/users/{user_id}/status")
    def user_status(user_id: uuid.UUID, body: S.UserStatus, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.user_status(db, identity, user_id, body.status, request.state.request_id),
        )

    @app.post("/api/admin/projects/{project_id}/members/{user_id}/restore")
    def restore_member(project_id: uuid.UUID, user_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.restore_member(db, identity, project_id, user_id, request.state.request_id),
        )

    @app.post("/api/admin/devices/{device_id}/revoke")
    def revoke_device(device_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: hub.revoke_device(db, identity, device_id, request.state.request_id),
        )

    @app.get("/api/audit")
    def audit(request: Request, db: DB, project_id: uuid.UUID | None = None):
        identity = actor(request, db)
        statement = select(Audit).order_by(Audit.created_at.desc()).limit(100)
        if identity.user.platform_role != "superadmin":
            if not project_id:
                raise HubError("PROJECT_NOT_ACCESSIBLE", 404)
            hub.manage_members(db, identity, project_id)
            hub.project(db, identity, project_id, "audit:read")
        if project_id:
            statement = statement.where(Audit.project_id == project_id)
        return {
            "items": [
                {
                    "id": str(e.id),
                    "action": e.action,
                    "object_id": e.object_id,
                    "project_id": str(e.project_id) if e.project_id else None,
                    "request_id": e.request_id,
                    "details": e.details,
                    "created_at": e.created_at.isoformat(),
                }
                for e in db.scalars(statement)
            ]
        }

    @app.get("/api/setup/status")
    def setup_status(db: DB):
        return {
            "setup_required": setup.required(db),
            "setup_enabled": bool(cfg.bootstrap_token.get_secret_value()),
            "active_origin": cfg.public_url,
        }

    def setup_identity(request, db):
        supplied = request.headers.get("X-Setup-Token", "")
        rate_limit(
            security_sessions,
            hub.secret,
            f"setup-access:{request.client.host if request.client else 'local'}",
            60,
            300,
        )
        result = proof(request, supplied)
        coordinate(db)
        setup.guard(db, supplied)
        return result

    @app.get("/api/setup/configuration")
    def setup_configuration(request: Request, db: DB):
        setup_identity(request, db)
        return hub.site.view(db)

    @app.put("/api/setup/configuration")
    def save_setup_configuration(body: S.SiteConfigurationInput, request: Request, db: DB):
        setup_identity(request, db)
        result = hub.site.write(db, body)
        hub.audit(
            db, None, "setup.configuration_saved", "site", request.state.request_id, version=result["version"]
        )
        return result

    @app.post("/api/setup/challenges")
    def setup_challenge(body: S.BootstrapStart, request: Request, db: DB):
        jwk, thumb = setup_identity(request, db)
        rate_limit(security_sessions, hub.secret, f"setup-mail:{str(body.email).lower()}", 5, 3600)
        return setup.challenge(db, body, jwk, thumb, request.state.request_id)

    @app.post("/api/setup/complete")
    def complete_setup(body: S.BootstrapComplete, request: Request, db: DB):
        _, thumb = setup_identity(request, db)
        return setup.complete(db, body, thumb, request.state.request_id)

    @app.get("/api/admin/settings")
    def site_settings(request: Request, db: DB):
        require_root(actor(request, db))
        return hub.site.view(db)

    @app.put("/api/admin/settings")
    def save_site_settings(body: S.SiteConfigurationInput, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)

        def save():
            result = hub.site.write(db, body, identity.user.id)
            hub.audit(
                db,
                identity.user.id,
                "site.configuration_saved",
                "site",
                request.state.request_id,
                version=result["version"],
            )
            return result

        return mutate(request, db, identity, body, save)

    @app.post("/api/admin/settings/test-email")
    def test_email(body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        require_root(identity)
        rate_limit(security_sessions, hub.secret, f"mail-test:{identity.user.id}", 5, 3600)
        enqueue_mail(db, cfg, identity.user.email)
        hub.audit(db, identity.user.id, "site.test_email_queued", "site", request.state.request_id)
        return {"sent": True, "recipient": identity.user.email}

    @app.get("/api/admin/users")
    def users(request: Request, db: DB, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        require_root(actor(request, db))
        return {
            "items": [
                user_view(u)
                for u in db.scalars(select(User).order_by(User.login).offset(offset).limit(limit))
            ]
        }

    @app.get("/api/admin/devices")
    def devices(
        request: Request,
        db: DB,
        user_id: uuid.UUID | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        require_root(actor(request, db))
        statement = (
            select(Device, User.login).join(User, Device.user_id == User.id).order_by(Device.created_at)
        )
        if user_id:
            statement = statement.where(Device.user_id == user_id)
        return {
            "items": [device_view(d, login) for d, login in db.execute(statement.offset(offset).limit(limit))]
        }

    @app.get("/api/me/devices")
    def own_devices(request: Request, db: DB):
        identity = actor(request, db)
        return {
            "items": [
                device_view(d, identity.user.login)
                for d in db.scalars(
                    select(Device)
                    .where(Device.user_id == identity.user.id)
                    .order_by(Device.created_at)
                    .limit(100)
                )
            ]
        }

    @app.get("/api/projects/{project_id}/members")
    def project_members(
        project_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        identity = actor(request, db)
        hub.manage_members(db, identity, project_id)
        rows = db.execute(
            select(Membership, User)
            .join(User, Membership.user_id == User.id)
            .where(Membership.project_id == project_id)
            .order_by(User.login)
            .offset(offset)
            .limit(limit)
        )
        return {
            "items": [
                {**user_view(u), "membership_role": m.role, "membership_status": m.status} for m, u in rows
            ]
        }

    @app.get("/api/projects/{project_id}/member-devices")
    def project_devices(
        project_id: uuid.UUID,
        request: Request,
        db: DB,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        identity = actor(request, db)
        hub.manage_members(db, identity, project_id)
        rows = db.execute(
            select(Device, User.login)
            .join(User, Device.user_id == User.id)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.project_id == project_id)
            .order_by(Device.created_at)
            .offset(offset)
            .limit(limit)
        )
        return {"items": [device_view(d, login) for d, login in rows]}

    @app.get("/api/projects/{project_id}/grants")
    def project_grants(project_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        hub.manage_members(db, identity, project_id)
        return {
            "items": [
                grant_view(g)
                for g in db.scalars(
                    select(Grant)
                    .where(Grant.project_id == project_id)
                    .order_by(Grant.created_at.desc())
                    .limit(100)
                )
            ]
        }

    @app.post("/api/auth/select-project")
    def select_project(body: S.StartSession, request: Request, db: DB):
        identity = actor(request, db, grant_id=body.grant_id)
        grant = db.get(Grant, body.grant_id) if body.grant_id else None
        if not grant or grant.user_id != identity.user.id or grant.device_id != identity.device.id:
            raise HubError("GRANT_NOT_ACCESSIBLE", 404)
        result = hub.issue_session(db, identity.user, identity.device, grant)
        db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == identity.session.family_id)
            .values(state="revoked")
        )
        return result

    @app.post("/api/auth/project-sessions")
    def project_session(body: S.StartSession, request: Request, db: DB):
        # Independent project families let desktop bridges coexist without changing others.
        identity = actor(request, db, grant_id=body.grant_id)
        grant = db.get(Grant, body.grant_id) if body.grant_id else None
        if not grant or grant.user_id != identity.user.id or grant.device_id != identity.device.id:
            raise HubError("GRANT_NOT_ACCESSIBLE", 404)
        return hub.issue_session(db, identity.user, identity.device, grant)

    @app.get("/api/projects/{project_id}/progress")
    def project_progress(
        project_id: uuid.UUID,
        request: Request,
        db: DB,
        after: int = Query(0, ge=0, le=9223372036854775807),
        limit: int = Query(50, ge=1, le=100),
    ):
        hub.project(db, actor(request, db), project_id)
        return progress.page(db, project_id, after, limit)

    @app.get("/api/projects/{project_id}/tasks")
    def task_list(project_id: uuid.UUID, request: Request, db: DB,
                  offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100), repository_id: uuid.UUID | None = None):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        return tasks.page(db, project_id, offset, limit, repository_id=repository_id)

    @app.get("/api/projects/{project_id}/roles")
    def project_roles(project_id: uuid.UUID, request: Request, db: DB, language: str = Query("zh", pattern="^(zh|en)$")):
        hub.project(db, actor(request, db), project_id)
        return roles.catalog(language)

    @app.get("/api/projects/{project_id}/policy")
    def project_policy(project_id: uuid.UUID, request: Request, db: DB):
        hub.project(db, actor(request, db), project_id)
        return work_policy.current(db, project_id)

    @app.put("/api/projects/{project_id}/policy")
    def publish_policy(project_id: uuid.UUID, body: S.PolicyInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        return mutate(request, db, identity, body,
                      lambda: work_policy.publish(hub, db, identity, project_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/contexts")
    def prepare_work_context(project_id: uuid.UUID, task_id: uuid.UUID, body: S.LeaseInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        task, run = tasks.live_run(db, identity, project_id, task_id, body)
        query = (task.title + " " + task.description)[:200]
        identity = semantic_query(request, db, identity, query)
        hub.project(db, identity, project_id, "tasks:write")
        task, run = tasks.live_run(db, identity, project_id, task_id, body)
        result = mutate(request, db, identity, body,
                        lambda: work_policy.prepare(hub, db, identity, project_id, task_id, body, request.state.request_id))
        # A cached preparation must not re-export data after source permissions changed.
        work_policy.require_context(db, identity, task, run, uuid.UUID(result["id"]), acknowledged=False)
        return result

    @app.post("/api/projects/{project_id}/tasks/{task_id}/context-acknowledgments")
    def acknowledge_work_context(project_id: uuid.UUID, task_id: uuid.UUID, body: S.ContextAcknowledgment,
                                 request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        task, run = tasks.live_run(db, identity, project_id, task_id, body)
        work_policy.require_context(db, identity, task, run, body.context_id, acknowledged=False)
        return mutate(request, db, identity, body,
                      lambda: work_policy.acknowledge(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/tasks/{task_id}/contexts/{context_id}/status")
    def work_context_status(project_id: uuid.UUID, task_id: uuid.UUID, context_id: uuid.UUID,
                            request: Request, db: DB, run_id: uuid.UUID, lease_epoch: int = Query(ge=1)):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        task, run = tasks.live_run(db, identity, project_id, task_id, S.LeaseInput(run_id=run_id, lease_epoch=lease_epoch))
        row, _ = work_policy.require_context(db, identity, task, run, context_id)
        return {"can_continue": True, "context_id": str(row.id), "content_hash": row.content_hash,
                "memory": memory.status(db, project_id, task.repository_id)}

    @app.get("/api/projects/{project_id}/memory/knowledge")
    def knowledge_search(project_id: uuid.UUID, request: Request, db: DB, query: str = Query("", max_length=200),
                         repository_id: uuid.UUID | None = None, include_history: bool = False,
                         search_mode: Literal["hybrid", "keyword"] = "hybrid",
                         offset: int = Query(0, ge=0, le=100000), limit: int = Query(12, ge=1, le=50)):
        from . import knowledge
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        rate_limit(security_sessions, hub.secret, f"memory-read:{identity.user.id}", 180, 60)
        identity = semantic_query(request, db, identity, query, search_mode)
        hub.project(db, identity, project_id)
        return knowledge.search(db, project_id, S.MemorySearch(query=query, repository_id=repository_id,
                                include_history=include_history, offset=offset, limit=limit, search_mode=search_mode))

    @app.post("/api/projects/{project_id}/memory/knowledge")
    def knowledge_proposal(project_id: uuid.UUID, body: S.KnowledgeInput, request: Request, db: DB):
        from . import knowledge
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        rate_limit(security_sessions, hub.secret, f"knowledge-write:{identity.user.id}", 60, 60)
        knowledge.source_for_proposal(db, project_id, body)
        return mutate(request, db, identity, body,
                      lambda: knowledge.propose(hub, db, identity, project_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/memory/knowledge/{fact_id}/decisions")
    def knowledge_decision(project_id: uuid.UUID, fact_id: uuid.UUID, body: S.KnowledgeDecision,
                           request: Request, db: DB):
        from . import knowledge
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        knowledge.get(db, project_id, fact_id, body.repository_id)
        if body.replaces_id:
            knowledge.get(db, project_id, body.replaces_id, body.repository_id)
        return mutate(request, db, identity, body,
                      lambda: knowledge.decide(hub, db, identity, project_id, fact_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/memory/status")
    def memory_status(project_id: uuid.UUID, request: Request, db: DB, repository_id: uuid.UUID | None = None):
        hub.project(db, actor(request, db), project_id)
        return memory.status(db, project_id, repository_id)

    @app.get("/api/projects/{project_id}/memory")
    def search_memory(project_id: uuid.UUID, request: Request, db: DB, query: str = Query("", max_length=200),
                      repository_id: uuid.UUID | None = None, include_history: bool = False,
                      search_mode: Literal["hybrid", "keyword"] = "hybrid",
                      offset: int = Query(0, ge=0, le=100000), limit: int = Query(12, ge=1, le=50)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        rate_limit(security_sessions, hub.secret, f"memory-read:{identity.user.id}", 180, 60)
        identity = semantic_query(request, db, identity, query, search_mode)
        hub.project(db, identity, project_id)
        return memory.search(db, project_id, S.MemorySearch(query=query, repository_id=repository_id,
                             include_history=include_history, offset=offset, limit=limit, search_mode=search_mode))

    @app.get("/api/projects/{project_id}/memory/{card_id}/original")
    def memory_original(project_id: uuid.UUID, card_id: uuid.UUID, request: Request, db: DB,
                        repository_id: uuid.UUID | None = None, start_line: int = Query(1, ge=1, le=1000000),
                        limit: int = Query(100, ge=1, le=200), start_char: int = Query(0, ge=0, le=300000)):
        hub.project(db, actor(request, db), project_id)
        return memory.original(db, project_id, card_id, start_line, limit, repository_id=repository_id, start_char=start_char)

    @app.post("/api/projects/{project_id}/memory/retry")
    def retry_memory(project_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        return mutate(request, db, identity, body,
                      lambda: memory.retry_failed(hub, db, identity, project_id, request.state.request_id))

    @app.post("/api/projects/{project_id}/memory/{card_id}/decisions")
    def memory_decision(project_id: uuid.UUID, card_id: uuid.UUID, body: S.MemoryDecision, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        memory.read_card(db, project_id, card_id)
        return mutate(request, db, identity, body,
                      lambda: memory.decide(hub, db, identity, project_id, card_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/activity")
    def recent_activity(project_id: uuid.UUID, request: Request, db: DB,
                        before: int = Query(0, ge=0, le=9223372036854775807), limit: int = Query(20, ge=1, le=100)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        return progress.recent(db, project_id, before, limit)

    @app.get("/api/projects/{project_id}/tasks/{task_id}")
    def task_detail(project_id: uuid.UUID, task_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        return tasks.detail(db, project_id, task_id)

    @app.post("/api/projects/{project_id}/tasks")
    def create_task(project_id: uuid.UUID, body: S.NewTask, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        return mutate(request, db, identity, body,
                      lambda: tasks.create(hub, db, identity, project_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/dependencies")
    def task_dependencies(project_id: uuid.UUID, task_id: uuid.UUID, body: S.TaskDependencies,
                          request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        return mutate(request, db, identity, body,
                      lambda: tasks.update_dependencies(hub, db, identity, project_id, task_id, body,
                                                        request.state.request_id))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/claim")
    def claim_task(project_id: uuid.UUID, task_id: uuid.UUID, body: S.TaskClaim, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:claim")
        hub.project(db, identity, project_id, "tasks:write")
        return mutate(request, db, identity, body,
                      lambda: tasks.claim(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/heartbeat")
    def heartbeat_task(project_id: uuid.UUID, task_id: uuid.UUID, body: S.LeaseInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:claim")
        return mutate(request, db, identity, body,
                      lambda: tasks.heartbeat(hub, db, identity, project_id, task_id, body))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/release")
    def release_task(project_id: uuid.UUID, task_id: uuid.UUID, body: S.TaskRelease, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:claim")
        return mutate(request, db, identity, body,
                      lambda: tasks.release(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/tasks/{task_id}/worklogs")
    def write_worklog(project_id: uuid.UUID, task_id: uuid.UUID, body: S.WorklogInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        rate_limit(security_sessions, hub.secret, f"worklog:{identity.user.id}", 120, 60)
        return mutate(request, db, identity, body,
                      lambda: tasks.write_log(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/tasks/{task_id}/worklogs")
    def worklog_list(project_id: uuid.UUID, task_id: uuid.UUID, request: Request, db: DB,
                     offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=50)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        result = tasks.logs(db, project_id, task_id, offset, limit)
        result["visibility"] = "project" if identity.user.platform_role == "superadmin" or "logs:read" in identity.grant.capabilities else "own"
        return result

    @app.post("/api/projects/{project_id}/tasks/{task_id}/submit")
    def submit_task(project_id: uuid.UUID, task_id: uuid.UUID, body: S.TaskSubmit, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        return mutate(request, db, identity, body,
                      lambda: tasks.submit(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/tasks/{task_id}/worklog-index")
    def worklog_index(project_id: uuid.UUID, task_id: uuid.UUID, request: Request, db: DB,
                      offset: int = Query(0, ge=0, le=100000), limit: int = Query(10, ge=1, le=50)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        result = tasks.log_index(db, project_id, task_id, offset, limit)
        result["visibility"] = "project" if identity.user.platform_role == "superadmin" or "logs:read" in identity.grant.capabilities else "own"
        return result

    @app.get("/api/projects/{project_id}/tasks/{task_id}/worklogs/{log_id}")
    def read_worklog(project_id: uuid.UUID, task_id: uuid.UUID, log_id: uuid.UUID, request: Request, db: DB):
        hub.project(db, actor(request, db), project_id)
        return tasks.read_log(db, project_id, task_id, log_id)

    @app.post("/api/projects/{project_id}/tasks/{task_id}/decisions")
    def decide_task(project_id: uuid.UUID, task_id: uuid.UUID, body: S.GateDecision, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, {"review":"tasks:review", "test":"tasks:test"}.get(body.gate, "tasks:publish"))
        if body.gate not in tasks.ADMISSION:
            hub.project(db, identity, project_id, "logs:read")
        return mutate(request, db, identity, body,
                      lambda: tasks.decide(hub, db, identity, project_id, task_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/documents/repository")
    def document_repository(project_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        manager = bool(db.scalar(text("SELECT hub.can_manage_documents(:p)"), {"p": project_id}))
        hub.project(db, identity, project_id, "documents:manage" if manager else "documents:read")
        repositories.source_access(hub, db, identity, project_id, manage=manager)
        return documents.repository_view(db, project_id, manager=manager)

    @app.put("/api/projects/{project_id}/documents/repository")
    def configure_documents(project_id: uuid.UUID, body: S.DocumentRepositoryInput, request: Request, db: DB):
        identity = actor(request, db)
        documents.require_manager(hub, db, identity, project_id)
        repositories.source_access(hub, db, identity, project_id, manage=True)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: documents.configure(hub, db, identity, project_id, body, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/documents/syncs")
    def sync_documents(project_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        documents.require_manager(hub, db, identity, project_id)
        repositories.source_access(hub, db, identity, project_id, manage=True)
        rate_limit(security_sessions, hub.secret, f"documents-sync:{project_id}", 30, 3600)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: documents.enqueue(hub, db, identity, project_id, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/documents/search")
    def search_documents(project_id: uuid.UUID, body: S.DocumentSearch, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "documents:read")
        rate_limit(security_sessions, hub.secret, f"documents-read:{identity.user.id}", 180, 60)
        return documents.search(db, project_id, body)

    @app.post("/api/projects/{project_id}/documents/read")
    def read_document(project_id: uuid.UUID, body: S.DocumentRead, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "documents:read")
        rate_limit(security_sessions, hub.secret, f"documents-read:{identity.user.id}", 180, 60)
        return documents.read(db, project_id, body)

    @app.get("/api/projects/{project_id}/documents/sources")
    def list_document_sources(project_id: uuid.UUID, request: Request, db: DB):
        hub.project(db, actor(request, db), project_id, "documents:read")
        return repositories.document_sources(db, project_id)

    @app.get("/api/projects/{project_id}/repositories/{repository_id}/documents/repository")
    def repository_document_index(project_id: uuid.UUID, repository_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        manager = bool(db.scalar(text("SELECT hub.can_manage_documents(:p)"), {"p": project_id})) and repositories.permitted(db, project_id, repository_id, "code:read")
        repositories.source_access(hub, db, identity, project_id, repository_id, manage=manager)
        return documents.repository_view(db, project_id, manager=manager, repository_id=repository_id)

    @app.put("/api/projects/{project_id}/repositories/{repository_id}/documents/repository")
    def configure_repository_documents(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.DocumentRepositoryInput, request: Request, db: DB):
        identity = actor(request, db)
        documents.require_manager(hub, db, identity, project_id)
        repositories.source_access(hub, db, identity, project_id, repository_id, manage=True)
        return mutate(request, db, identity, body, lambda: documents.configure(
            hub, db, identity, project_id, body, request.state.request_id, repository_id=repository_id))

    @app.post("/api/projects/{project_id}/repositories/{repository_id}/documents/syncs")
    def sync_repository_documents(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        documents.require_manager(hub, db, identity, project_id)
        repositories.source_access(hub, db, identity, project_id, repository_id, manage=True)
        rate_limit(security_sessions, hub.secret, f"documents-sync:{project_id}", 30, 3600)
        return mutate(request, db, identity, body, lambda: documents.enqueue(
            hub, db, identity, project_id, request.state.request_id, repository_id=repository_id))

    @app.post("/api/projects/{project_id}/repositories/{repository_id}/documents/search")
    def search_repository_documents(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.DocumentSearch, request: Request, db: DB):
        identity = actor(request, db)
        repositories.source_access(hub, db, identity, project_id, repository_id)
        rate_limit(security_sessions, hub.secret, f"documents-read:{identity.user.id}", 180, 60)
        return documents.search(db, project_id, body, repository_id=repository_id)

    @app.post("/api/projects/{project_id}/repositories/{repository_id}/documents/read")
    def read_repository_document(project_id: uuid.UUID, repository_id: uuid.UUID, body: S.DocumentRead, request: Request, db: DB):
        identity = actor(request, db)
        repositories.source_access(hub, db, identity, project_id, repository_id)
        rate_limit(security_sessions, hub.secret, f"documents-read:{identity.user.id}", 180, 60)
        return documents.read(db, project_id, body, repository_id=repository_id)

    @app.post("/api/projects/{project_id}/progress")
    def report_progress(project_id: uuid.UUID, body: S.ProgressReport, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        rate_limit(security_sessions, hub.secret, f"progress:{identity.user.id}", 120, 60)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: progress.report(hub, db, identity, project_id, body, request.state.request_id),
        )

    @app.get("/api/projects/{project_id}/progress/stream")
    def stream_progress(
        project_id: uuid.UUID,
        request: Request,
        after: int = Query(0, ge=0, le=9223372036854775807),
    ):
        # No dependency transaction or identity lock stays open while the stream waits.
        value = token(request)
        _, thumb = proof(request, value)
        with sessions.begin() as db:
            hub.lock_session(db, value, thumb, project_ids=[project_id], exclusive=False)
            hub.project(db, hub.authenticate(db, value, thumb), project_id)

        def batch(cursor):
            with sessions.begin() as db:
                hub.lock_session(db, value, thumb, project_ids=[project_id], exclusive=False)
                project = hub.project(db, hub.authenticate(db, value, thumb), project_id)
                return progress.page(db, project_id, cursor, 100) | {"resource_visibility": repositories.visibility(db, project)}

        async def events():
            cursor, last_ping, ready, visibility = after, 0, False, None
            while not await request.is_disconnected():
                try:
                    result = await asyncio.to_thread(batch, cursor)
                except HubError as exc:
                    yield "event: access_status\ndata: " + json.dumps({"code": exc.code}) + "\n\n"
                    return
                if result["resource_visibility"] != visibility:
                    visibility = result["resource_visibility"]
                    yield "event: resource_visibility\ndata: " + json.dumps(visibility) + "\n\n"
                for entry in result["items"]:
                    yield f"id: {entry['sequence']}\nevent: progress\ndata: {json.dumps(entry)}\n\n"
                    cursor = int(entry["sequence"])
                if not result["has_more"] and not ready:
                    yield "event: ready\ndata: {}\n\n"
                    ready = True
                if time.monotonic() - last_ping >= 10:
                    yield ": heartbeat\n\n"
                    last_ping = time.monotonic()
                if not result["has_more"]:
                    await asyncio.sleep(2)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/auth/logout")
    def logout(body: S.Empty, request: Request, db: DB):
        identity = actor(request, db)
        db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == identity.session.family_id)
            .values(state="revoked")
        )
        db.execute(delete(Enrollment).where(Enrollment.device_id == identity.device.id))
        hub.audit(db, identity.user.id, "session.logged_out", identity.session.id, request.state.request_id)
        return {"logged_out": True}

    @app.post("/api/me/email-challenges")
    def bind_email_challenge(body: S.EmailChangeRequest, request: Request, db: DB):
        identity = actor(request, db)
        rate_limit(security_sessions, hub.secret, f"bind-email:{identity.user.id}", 5, 3600)
        return email_change_start(hub, db, identity, body, request.state.request_id)

    @app.post("/api/me/email-verifications")
    def bind_email_verification(body: S.VerifyEmail, request: Request, db: DB):
        identity = actor(request, db)
        return email_change_complete(hub, db, identity, body, request.state.request_id)

    static = Path(__file__).parent / "static"
    git_gateway.install(app, hub, sessions, db_dependency)
    work_archives.install(app, hub, actor, token, db_dependency)
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(static / "index.html")

    return app
