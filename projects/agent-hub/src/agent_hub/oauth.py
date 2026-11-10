"""OAuth 2 authorization for preapproved, device-bound clients. No dynamic URL fetching."""

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field, model_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from starlette.concurrency import run_in_threadpool

from . import repositories
from . import schemas as S
from .db import context, coordinate
from .models import AuthSession, Base, Device, Grant, Identity, User
from .security import HubError, b64, now, rate_limit
from .service import Principal, require_root

SCOPES = {"mcp:read", "mcp:coordinate"}


class Client(Identity, Base):
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approver_device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    grant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_grants.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_repositories.id"))
    redirect_uris: Mapped[list] = mapped_column(JSONB)
    scopes: Mapped[list] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Authorization(Identity, Base):
    __tablename__ = "oauth_requests"
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oauth_clients.id"))
    client_version: Mapped[int] = mapped_column(Integer)
    redirect_uri: Mapped[str] = mapped_column(String(2000))
    resource: Mapped[str] = mapped_column(String(1000))
    scopes: Mapped[list] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(2048))
    challenge: Mapped[str] = mapped_column(String(43))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("auth_sessions.id"))
    code_digest: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Identity, Base):
    __tablename__ = "oauth_sessions"
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oauth_clients.id"))
    client_version: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_sessions.id"))
    resource: Mapped[str] = mapped_column(String(1000))
    scopes: Mapped[list] = mapped_column(JSONB)
    family_id: Mapped[uuid.UUID]
    access_digest: Mapped[str] = mapped_column(String(64))
    refresh_digest: Mapped[str] = mapped_column(String(64))
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="active")


class Failure(HubError):
    def __init__(self, error="invalid_grant", status=400, *, persist=False):
        super().__init__("OAUTH_" + error.upper(), status, persist=persist)
        self.error = error


class Register(S.Input):
    name: str = Field(min_length=1, max_length=200)
    user_id: uuid.UUID
    device_id: uuid.UUID
    grant_id: uuid.UUID | None = None
    project_id: uuid.UUID
    repository_id: uuid.UUID
    redirect_uris: list[str] = Field(min_length=1, max_length=5)
    scopes: list[Literal["mcp:read", "mcp:coordinate"]] = Field(default_factory=lambda: ["mcp:read"])
    reason: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def valid_redirects(self):
        if len(set(self.redirect_uris)) != len(self.redirect_uris) or "mcp:read" not in self.scopes:
            raise ValueError("Unique redirects and nonempty scopes are required")
        for value in self.redirect_uris:
            url = urlsplit(value)
            if (
                len(value) > 2000
                or not url.hostname
                or url.username
                or url.password
                or url.fragment
                or any(ord(c) < 33 for c in value)
                or url.scheme not in {"http", "https"}
                or url.scheme == "http"
                and url.hostname not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise ValueError("Use exact HTTPS redirects or exact loopback HTTP redirects")
            _ = url.port
        return self


class Consent(S.Input):
    approve: bool


def resource(hub, client):
    return (
        hub.settings.public_url.rstrip("/")
        + f"/mcp/projects/{client.project_id}/repositories/{client.repository_id}"
    )


def origin_allowed(hub, db, sent):
    if not sent or sent == hub.settings.public_url.rstrip("/"):
        return True
    return any(
        sent == (urlsplit(uri).scheme + "://" + urlsplit(uri).netloc)
        for redirects in db.scalars(select(Client.redirect_uris).where(Client.status == "active"))
        for uri in redirects
    )


def cors(request):
    return (
        {"Access-Control-Allow-Origin": request.headers["origin"], "Vary": "Origin"}
        if request.headers.get("origin")
        else {}
    )


def client_view(hub, row):
    return {
        "id": str(row.id),
        "client_id": row.client_id,
        "name": row.name,
        "status": row.status,
        "version": row.version,
        "user_id": str(row.user_id),
        "device_id": str(row.device_id),
        "grant_id": str(row.grant_id) if row.grant_id else None,
        "project_id": str(row.project_id),
        "repository_id": str(row.repository_id),
        "redirect_uris": row.redirect_uris,
        "scopes": row.scopes,
        "resource": resource(hub, row),
        "pkce_required": True,
    }


def eligible(hub, db, client, *, session=None):
    if client.status != "active":
        raise Failure()
    user, device = db.get(User, client.user_id), db.get(Device, client.device_id)
    if not user or not device or device.user_id != user.id:
        raise Failure()
    try:
        hub.validate_identity(user, device, session)
        if user.platform_role == "superadmin" and client.grant_id is not None:
            raise Failure()
        grant = db.get(Grant, client.grant_id) if client.grant_id else None
        if user.platform_role != "superadmin":
            hub.validate_grant(db, user, device, grant)
        context(db, user.id, device.id, client.grant_id)
        identity = Principal(user, device, session, grant)
        hub.project(db, identity, client.project_id)
        repositories.require(db, client.project_id, client.repository_id)
        if session:
            if (session.user_id, session.device_id, session.grant_id) != (
                user.id,
                device.id,
                client.grant_id,
            ):
                raise Failure()
            if not db.scalar(
                select(AuthSession.id)
                .where(
                    AuthSession.family_id == session.family_id,
                    AuthSession.state == "active",
                    AuthSession.refresh_expires_at > now(),
                )
                .limit(1)
            ):
                raise Failure()
        return identity
    except HubError as error:
        if isinstance(error, Failure):
            raise
        raise Failure() from None


def create_client(hub, db, identity, body, request_id):
    require_root(identity)
    client = Client(
        client_id="hub_" + secrets.token_urlsafe(32),
        status="active",
        version=1,
        author_id=identity.user.id,
        approver_device_id=identity.device.id,
        **body.model_dump(exclude={"reason"}, mode="python"),
    )
    eligible(hub, db, client)
    context(db, identity.user.id, identity.device.id, identity.grant.id if identity.grant else None)
    db.add(client)
    db.flush()
    hub.audit(
        db,
        identity.user.id,
        "oauth.client_approved",
        client.id,
        request_id,
        client.project_id,
        reason=body.reason,
    )
    return client_view(hub, client)


def authorize(hub, db, params):
    required = {
        "client_id",
        "redirect_uri",
        "response_type",
        "code_challenge",
        "code_challenge_method",
        "resource",
    }
    if not required <= set(params) or params["response_type"] != "code":
        raise Failure("invalid_request")
    client = db.scalar(
        select(Client).where(Client.client_id == params["client_id"], Client.status == "active")
    )
    if (
        not client
        or params["redirect_uri"] not in client.redirect_uris
        or params["resource"] != resource(hub, client)
    ):
        raise Failure("invalid_request")
    if params["code_challenge_method"] != "S256" or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", params["code_challenge"]
    ):
        raise Failure("invalid_request")
    scopes = params.get("scope", "mcp:read").split()
    if "mcp:read" not in scopes or not set(scopes) <= set(client.scopes):
        raise Failure("invalid_scope")
    state = params.get("state", "")
    if len(state) > 2048 or any(ord(c) < 32 for c in state):
        raise Failure("invalid_request")
    row = Authorization(
        client_id=client.id,
        client_version=client.version,
        redirect_uri=params["redirect_uri"],
        resource=params["resource"],
        scopes=sorted(set(scopes)),
        state=state,
        challenge=params["code_challenge"],
        expires_at=now() + timedelta(minutes=5),
    )
    db.add(row)
    db.flush()
    return row


def request_for(hub, db, identity, request_id, *, require_selected=True):
    row = db.get(Authorization, request_id)
    client = db.get(Client, row.client_id) if row else None
    if (
        not row
        or not client
        or row.decided_at
        or row.expires_at <= now()
        or client.version != row.client_version
        or (client.user_id, client.device_id) != (identity.user.id, identity.device.id)
        or require_selected
        and client.grant_id != (identity.grant.id if identity.grant else None)
    ):
        raise Failure("access_denied", 403)
    selected = client.grant_id == (identity.grant.id if identity.grant else None)
    eligible(hub, db, client, session=identity.session if selected else None)
    return row, client


def consent(hub, db, identity, request_id, approve):
    row, client = request_for(hub, db, identity, request_id)
    row.decided_at, row.session_id = now(), identity.session.id
    query = {"iss": hub.settings.public_url.rstrip("/"), "state": row.state}
    if approve:
        code = secrets.token_urlsafe(48)
        row.code_digest = hub.hash("oauth-code:" + code)
        query["code"] = code
    else:
        row.consumed_at = now()
        query["error"] = "access_denied"
    url = urlsplit(row.redirect_uri)
    location = urlunsplit(
        (url.scheme, url.netloc, url.path, url.query + ("&" if url.query else "") + urlencode(query), "")
    )
    hub.audit(
        db,
        identity.user.id,
        "oauth.consent_" + ("approved" if approve else "denied"),
        client.id,
        str(request_id),
        client.project_id,
    )
    return {"redirect_uri": location}


def issue(hub, db, client, source, audience, scopes, *, family=None, refresh_expiry=None):
    if source is None:
        raise Failure()
    eligible(hub, db, client, session=source)
    access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    expiry = refresh_expiry or now() + timedelta(seconds=hub.settings.refresh_seconds)
    row = Session(
        client_id=client.id,
        client_version=client.version,
        session_id=source.id,
        resource=audience,
        scopes=scopes,
        family_id=family or uuid.uuid4(),
        access_digest=hub.hash("oauth-access:" + access),
        refresh_digest=hub.hash("oauth-refresh:" + refresh),
        access_expires_at=min(now() + timedelta(seconds=hub.settings.access_seconds), expiry),
        refresh_expires_at=expiry,
    )
    db.add(row)
    db.flush()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": max(0, int((row.access_expires_at - now()).total_seconds())),
        "scope": " ".join(scopes),
        "resource": audience,
    }


def exchange(hub, db, values):
    client = db.scalar(select(Client).where(Client.client_id == values.get("client_id")))
    if not client or client.status != "active" or values.get("resource") != resource(hub, client):
        raise Failure("invalid_client" if not client else "invalid_target")
    coordinate(
        db,
        user_ids=[client.user_id],
        project_ids=[client.project_id],
        resources=["oauth-client:" + str(client.id)],
    )
    db.refresh(client)
    if values.get("grant_type") == "authorization_code":
        row = db.scalar(
            select(Authorization)
            .where(Authorization.code_digest == hub.hash("oauth-code:" + values.get("code", "")))
            .with_for_update()
        )
        verifier = values.get("code_verifier", "")
        if (
            not row
            or row.client_id != client.id
            or row.client_version != client.version
            or row.consumed_at
            or not row.decided_at
            or row.expires_at <= now()
            or row.resource != values["resource"]
            or row.redirect_uri != values.get("redirect_uri")
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
            or b64(hashlib.sha256(verifier.encode()).digest()) != row.challenge
        ):
            raise Failure()
        source = db.get(AuthSession, row.session_id)
        if not source:
            raise Failure()
        result = issue(hub, db, client, source, row.resource, row.scopes)
        row.consumed_at = now()
        return result
    if values.get("grant_type") == "refresh_token":
        row = db.scalar(
            select(Session)
            .where(Session.refresh_digest == hub.hash("oauth-refresh:" + values.get("refresh_token", "")))
            .with_for_update()
        )
        if not row or row.client_id != client.id or row.resource != values["resource"]:
            raise Failure()
        if row.state == "rotated":
            db.execute(update(Session).where(Session.family_id == row.family_id).values(state="revoked"))
            raise Failure(persist=True)
        scopes = values.get("scope", " ".join(row.scopes)).split()
        if (
            row.state != "active"
            or row.refresh_expires_at <= now()
            or row.client_version != client.version
            or "mcp:read" not in scopes
            or not set(scopes) <= set(row.scopes)
        ):
            raise Failure()
        source = db.get(AuthSession, row.session_id)
        result = issue(
            hub,
            db,
            client,
            source,
            row.resource,
            sorted(set(scopes)),
            family=row.family_id,
            refresh_expiry=row.refresh_expires_at,
        )
        row.state = "rotated"
        return result
    raise Failure("unsupported_grant_type")


def authenticate(hub, db, token, audience):
    if not isinstance(token, str) or not 20 <= len(token) <= 256:
        raise Failure("invalid_token", 401)
    row = db.scalar(select(Session).where(Session.access_digest == hub.hash("oauth-access:" + token)))
    client = db.get(Client, row.client_id) if row else None
    if (
        not row
        or not client
        or row.resource != audience
        or row.state != "active"
        or row.access_expires_at <= now()
    ):
        raise Failure("invalid_token", 401)
    coordinate(
        db,
        user_ids=[client.user_id],
        project_ids=[client.project_id],
        resources=["oauth-client:" + str(client.id)],
        exclusive=False,
    )
    db.expire_all()
    db.refresh(client)
    db.refresh(row)
    if (
        client.version != row.client_version
        or row.state != "active"
        or row.access_expires_at <= now()
        or resource(hub, client) != audience
    ):
        raise Failure("invalid_token", 401)
    source = db.get(AuthSession, row.session_id)
    if not source:
        raise Failure("invalid_token", 401)
    try:
        identity = eligible(hub, db, client, session=source)
    except Failure:
        raise Failure("invalid_token", 401) from None
    return identity, client, row


def pairs(values):
    result = {}
    for key, value in values:
        if key in result or len(key) > 100 or len(value) > 4096:
            raise Failure("invalid_request")
        result[key] = value
    return result


def install(app, hub, actor, mutate, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]

    @app.exception_handler(Failure)
    async def failure(request, error):
        return JSONResponse(
            {"error": error.error},
            status_code=error.status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
            | (cors(request) if getattr(request.state, "oauth_origin_allowed", False) else {}),
        )

    @app.get("/.well-known/oauth-authorization-server")
    def metadata():
        origin = hub.settings.public_url.rstrip("/")
        return JSONResponse(
            {
                "issuer": origin,
                "authorization_endpoint": origin + "/oauth/authorize",
                "token_endpoint": origin + "/oauth/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": sorted(SCOPES),
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": False,
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.post("/api/admin/oauth/clients")
    def register(body: Register, request: Request, db: DB):
        identity = actor(request, db, users=[body.user_id], projects=[body.project_id])
        require_root(identity)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: create_client(hub, db, identity, body, request.state.request_id),
        )

    @app.get("/api/admin/oauth/clients")
    def clients(request: Request, db: DB):
        require_root(actor(request, db))
        return {
            "items": [
                client_view(hub, row)
                for row in db.scalars(select(Client).order_by(Client.created_at.desc()).limit(100))
            ]
        }

    @app.post("/api/admin/oauth/clients/{client_id}/revoke")
    def revoke(client_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db, resources=["oauth-client:" + str(client_id)])
        require_root(identity)
        row = db.get(Client, client_id)
        if not row:
            raise Failure("invalid_client", 404)
        row.status, row.version = "revoked", row.version + 1
        db.execute(update(Session).where(Session.client_id == row.id).values(state="revoked"))
        hub.audit(
            db, identity.user.id, "oauth.client_revoked", row.id, request.state.request_id, row.project_id
        )
        return {"revoked": True}

    @app.get("/oauth/authorize")
    def start(request: Request, db: DB):
        rate_limit(security_sessions, hub.secret, "oauth-auth-ip:" + request.client.host, 60, 3600)
        row = authorize(hub, db, pairs(request.query_params.multi_items()))
        return RedirectResponse("/?oauth_request=" + str(row.id), status_code=302)

    @app.get("/api/me/oauth/requests/{request_id}")
    def pending(request_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        row, client = request_for(hub, db, identity, request_id, require_selected=False)
        return {
            "request_id": str(row.id),
            "name": client.name,
            "resource": row.resource,
            "scopes": row.scopes,
            "redirect_uri": row.redirect_uri,
            "expires_at": row.expires_at.isoformat(),
            "required_grant_id": str(client.grant_id) if client.grant_id else None,
        }

    @app.get("/api/me/oauth/sessions")
    def own_sessions(request: Request, db: DB):
        identity = actor(request, db)
        rows = db.execute(
            select(Session, Client)
            .join(Client, Client.id == Session.client_id)
            .where(
                Client.user_id == identity.user.id,
                Session.state == "active",
                Session.refresh_expires_at > now(),
            )
            .order_by(Session.created_at.desc())
            .limit(100)
        )
        return {
            "items": [
                {
                    "id": str(row.id),
                    "name": client.name,
                    "device_id": str(client.device_id),
                    "resource": row.resource,
                    "scopes": row.scopes,
                    "expires_at": row.refresh_expires_at.isoformat(),
                }
                for row, client in rows
            ]
        }

    @app.post("/api/me/oauth/sessions/{session_id}/revoke")
    def revoke_own(session_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        row = db.get(Session, session_id)
        client = db.get(Client, row.client_id) if row else None
        if not client or client.user_id != identity.user.id:
            raise Failure("access_denied", 404)
        db.execute(update(Session).where(Session.family_id == row.family_id).values(state="revoked"))
        hub.audit(db, identity.user.id, "oauth.family_revoked", row.family_id, request.state.request_id)
        return {"revoked": True}

    @app.post("/api/me/oauth/requests/{request_id}")
    def approve(request_id: uuid.UUID, body: Consent, request: Request, db: DB):
        identity = actor(request, db, resources=["oauth-request:" + str(request_id)])
        return consent(hub, db, identity, request_id, body.approve)

    @app.post("/oauth/token")
    async def token(request: Request, db: DB):
        if not await run_in_threadpool(origin_allowed, hub, db, request.headers.get("origin")):
            return JSONResponse({"error": "access_denied"}, status_code=403)
        request.state.oauth_origin_allowed = True
        await run_in_threadpool(
            rate_limit, security_sessions, hub.secret, "oauth-token-ip:" + request.client.host, 120, 3600
        )
        if (
            request.query_params
            or request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded"
        ):
            raise Failure("invalid_request")
        content = bytearray()
        async for block in request.stream():
            content.extend(block)
            if len(content) > 8192:
                raise Failure("invalid_request")
        try:
            values = pairs(parse_qsl(content.decode("utf-8"), strict_parsing=True, max_num_fields=20))
        except (UnicodeError, ValueError):
            raise Failure("invalid_request") from None
        if values.get("client_secret") or request.headers.get("Authorization"):
            raise Failure("invalid_client")
        return JSONResponse(
            await run_in_threadpool(exchange, hub, db, values),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"} | cors(request),
        )

    @app.options("/oauth/token")
    def token_preflight(request: Request, db: DB):
        if not origin_allowed(hub, db, request.headers.get("origin")):
            return JSONResponse({"error": "access_denied"}, status_code=403)
        return JSONResponse(
            {},
            status_code=200,
            headers=cors(request)
            | {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
            },
        )
