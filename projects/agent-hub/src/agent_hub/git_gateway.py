"""Git smart HTTP gateway; upstream credentials never leave the service.

Tickets authorize one repository and are revalidated for every HTTP exchange.
Write tickets can update only one live task branch to one recorded commit.
"""

import asyncio
import base64
import re
import secrets
import tempfile
import uuid
from datetime import timedelta

import httpx
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from starlette.background import BackgroundTask

from . import github_credentials, repositories, tasks, work_policy
from .db import context, coordinate
from .models import AuthSession, Device, Grant, RepositoryTicket, TaskRun, User, Worklog
from .schemas import LeaseInput
from .security import HubError, now, rate_limit
from .service import Principal

MAX_REQUEST = 64 * 1024 * 1024
MAX_RESPONSE = 256 * 1024 * 1024


def branch_for(task_id, run_id):
    return f"refs/heads/hub/{task_id}/{run_id}"


def recorded_checkpoint(db, actor, task, run_id, expected_commit):
    latest = db.scalar(
        select(Worklog)
        .where(Worklog.project_id == task.project_id, Worklog.run_id == run_id)
        .order_by(Worklog.created_at.desc(), Worklog.id)
        .limit(1)
    )
    if not latest or latest.outcome != "checkpoint" or latest.git_commit != expected_commit:
        raise HubError("GIT_RECORDED_CHECKPOINT_REQUIRED", 409)
    if task.execution_mode == "observed":
        work_policy.require_context(db, actor, task, db.get(TaskRun, run_id), latest.context_id)
    return latest


def issue(hub, db, actor, project_id, repository_id, body, request_id):
    repo = repositories.require(
        db, project_id, repository_id, "code:write" if body.action == "write" else "code:read"
    )
    if body.action == "write":
        hub.project(db, actor, project_id, "tasks:write")
        task, _ = tasks.live_run(db, actor, project_id, body.task_id, body)
        if task.repository_id != repo.id:
            raise HubError("TASK_REPOSITORY_MISMATCH", 409)
        recorded_checkpoint(db, actor, task, body.run_id, body.expected_commit)
    secret = secrets.token_urlsafe(40)
    db.execute(delete(RepositoryTicket).where(RepositoryTicket.expires_at < now()))
    ticket = RepositoryTicket(
        project_id=project_id,
        repository_id=repository_id,
        session_id=actor.session.id,
        user_id=actor.user.id,
        device_id=actor.device.id,
        digest=hub.hash(secret),
        action=body.action,
        run_id=body.run_id,
        lease_epoch=body.lease_epoch,
        expected_commit=body.expected_commit,
        expires_at=min(now() + timedelta(minutes=2), actor.session.access_expires_at),
    )
    db.add(ticket)
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "repository.ticket_issued",
        ticket.id,
        request_id,
        project_id,
        repository_id=repository_id,
        action_scope=body.action,
    )
    return {
        "ticket": secret,
        "expires_at": ticket.expires_at.isoformat(),
        "git_path": f"/api/git/{repository_id}.git",
        "branch": repo.branch,
        "write_ref": branch_for(body.task_id, body.run_id) if body.action == "write" else None,
    }


def authenticate(hub, db, repository_id, header, *, write=False):
    try:
        if len(header) > 512:
            raise ValueError()
        kind, encoded = header.split(" ", 1)
        username, secret = base64.b64decode(encoded, validate=True).decode("ascii").split(":", 1)
        if kind.lower() != "basic" or username != "agent-hub" or not 32 <= len(secret) <= 128:
            raise ValueError()
    except Exception as exc:
        raise HubError("GIT_AUTHENTICATION_REQUIRED", 401) from exc
    ticket = db.scalar(
        select(RepositoryTicket).where(
            RepositoryTicket.digest == hub.hash(secret), RepositoryTicket.repository_id == repository_id
        )
    )
    if not ticket:
        raise HubError("GIT_AUTHENTICATION_REQUIRED", 401)
    coordinate(db, user_ids=[ticket.user_id], project_ids=[ticket.project_id], exclusive=False)
    db.expire_all()
    if ticket.expires_at <= now() or write and ticket.action != "write":
        raise HubError("GIT_TICKET_EXPIRED_OR_INSUFFICIENT", 403)
    session = db.get(AuthSession, ticket.session_id)
    user, device = db.get(User, ticket.user_id), db.get(Device, ticket.device_id)
    if not session or session.state != "active" or session.access_expires_at <= now():
        raise HubError("GIT_TICKET_EXPIRED_OR_INSUFFICIENT", 403)
    hub.validate_identity(user, device)
    if session.user_epoch != user.auth_epoch or session.device_epoch != device.epoch:
        raise HubError("GIT_TICKET_EXPIRED_OR_INSUFFICIENT", 403)
    grant = db.get(Grant, session.grant_id) if session.grant_id else None
    context(db, user.id, device.id, session.grant_id)
    actor = Principal(user, device, session, grant)
    hub.project(db, actor, ticket.project_id)
    repo = repositories.require(db, ticket.project_id, repository_id, "code:write" if write else "code:read")
    if write:
        hub.project(db, actor, ticket.project_id, "tasks:write")
        run = db.get(TaskRun, ticket.run_id)
        if not run:
            raise HubError("TASK_LEASE_STALE", 409)
        task, _ = tasks.live_run(
            db,
            actor,
            ticket.project_id,
            run.task_id,
            LeaseInput(run_id=ticket.run_id, lease_epoch=ticket.lease_epoch),
        )
        if task.repository_id != repo.id:
            raise HubError("TASK_REPOSITORY_MISMATCH", 409)
        recorded_checkpoint(db, actor, task, ticket.run_id, ticket.expected_commit)
    return actor, repo, ticket


def validate_receive_commands(data, expected_ref, expected_commit):
    """Validate the complete command section before forwarding any pack bytes."""
    offset, commands = 0, []
    while offset + 4 <= len(data):
        try:
            size = int(data[offset : offset + 4], 16)
        except ValueError:
            break
        if size == 0:
            if len(commands) == 1:
                return
            break
        if size < 4 or size > 65520 or offset + size > len(data):
            break
        command = data[offset + 4 : offset + size]
        if not commands:
            command, _, capabilities = command.partition(b"\x00")
            if b"push-options" in capabilities or b"push-cert" in capabilities:
                break
        try:
            old, new, ref = command.rstrip(b"\n").decode("ascii").split(" ")
        except (ValueError, UnicodeError):
            break
        if (
            not re.fullmatch(r"[a-f0-9]{40}", old)
            or new != expected_commit
            or ref != expected_ref
            or new == "0" * 40
        ):
            break
        commands.append((old, new, ref))
        offset += size
    raise HubError("GIT_PUSH_SCOPE_DENIED", 403)


def install(app, hub, sessions, db_dependency):
    database = Depends(db_dependency)

    @app.api_route("/api/git/{repository_id}.git/{suffix:path}", methods=["GET", "POST"])
    async def proxy(repository_id: uuid.UUID, suffix: str, request: Request, db=database):
        rate_limit(
            app.state.security_sessions,
            hub.secret,
            f"git-ip:{request.client.host if request.client else 'local'}",
            120,
            60,
        )
        # No dumb HTTP, arbitrary paths, redirects, credentials or upstream hosts supplied by callers.
        if request.method == "GET" and suffix == "info/refs":
            if list(request.query_params.keys()) != ["service"]:
                raise HubError("GIT_REQUEST_INVALID", 400)
            service = request.query_params["service"]
        elif (
            request.method == "POST"
            and suffix in {"git-upload-pack", "git-receive-pack"}
            and not request.query_params
        ):
            service = suffix
        else:
            raise HubError("GIT_REQUEST_INVALID", 400)
        if service not in {"git-upload-pack", "git-receive-pack"}:
            raise HubError("GIT_REQUEST_INVALID", 400)
        write = service == "git-receive-pack"
        actor, repo, ticket = authenticate(
            hub, db, repository_id, request.headers.get("authorization", ""), write=write
        )
        transport = getattr(app.state, "git_transport", None)
        upstream_secret = await asyncio.to_thread(
            github_credentials.credential, hub, db, repo, write=write, transport=transport
        )
        expected_ref = branch_for(db.get(TaskRun, ticket.run_id).task_id, ticket.run_id) if write else None
        expected_commit = ticket.expected_commit
        ticket_header = request.headers.get("authorization", "")
        upstream_url = repo.repository_url + ".git/" + suffix
        if request.method == "GET":
            upstream_url += "?service=" + service
        headers = {"User-Agent": "AgentHub-GitGateway", "Accept-Encoding": "identity"}
        if upstream_secret:
            headers["Authorization"] = (
                "Basic " + base64.b64encode(("x-access-token:" + upstream_secret).encode()).decode()
            )
        if request.headers.get("git-protocol") == "version=2":
            headers["Git-Protocol"] = "version=2"
        hub.audit(
            db,
            actor.user.id,
            "repository.git_request",
            repository_id,
            request.state.request_id,
            repo.project_id,
            repository_id=repo.id,
            service=service,
            stage="authorized",
        )
        # Release identity locks before network I/O. Recheck before forwarding a write body.
        db.commit()
        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(120, connect=10),
            trust_env=False,
            follow_redirects=False,
        )
        upstream = None
        try:
            if request.method == "POST":
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != f"application/x-{service}-request"
                ):
                    raise HubError("GIT_REQUEST_INVALID", 400)
                encoding = request.headers.get("content-encoding", "identity").lower()
                if encoding != "identity" and (write or encoding != "gzip"):
                    raise HubError("GIT_REQUEST_INVALID", 400)
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_REQUEST:
                        raise HubError("GIT_REQUEST_TOO_LARGE", 413)
                    spool.write(chunk)
                spool.seek(0)
                if write:
                    validate_receive_commands(spool.read(65536), expected_ref, expected_commit)
                    spool.seek(0)
                authenticate(hub, db, repository_id, ticket_header, write=write)
                db.commit()
                headers["Content-Type"] = f"application/x-{service}-request"
                if encoding != "identity":
                    headers["Content-Encoding"] = encoding

            async def content():
                while chunk := spool.read(65536):
                    yield chunk

            upstream = await client.send(
                client.build_request(
                    request.method,
                    upstream_url,
                    headers=headers,
                    content=content() if request.method == "POST" else None,
                ),
                stream=True,
            )
            expected_type = f"application/x-{service}-" + (
                "advertisement" if request.method == "GET" else "result"
            )
            if (
                upstream.status_code != 200
                or upstream.headers.get("content-type", "").split(";")[0] != expected_type
            ):
                raise HubError("REPOSITORY_UPSTREAM_REJECTED", 502)
        except Exception as exc:
            spool.close()
            if upstream:
                await upstream.aclose()
            await client.aclose()
            if isinstance(exc, HubError):
                raise
            raise HubError("REPOSITORY_CONNECTION_FAILED", 502) from exc

        async def close():
            spool.close()
            await upstream.aclose()
            await client.aclose()

        async def response_body():
            size = 0
            try:
                async for chunk in upstream.aiter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise RuntimeError("Git response limit exceeded")
                    yield chunk
            finally:
                await close()

        return StreamingResponse(
            response_body(),
            media_type=expected_type,
            background=BackgroundTask(close),
            headers={"X-Accel-Buffering": "no"},
        )
