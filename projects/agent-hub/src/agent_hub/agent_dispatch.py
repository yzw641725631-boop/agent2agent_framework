"""Durable offers to explicitly registered recipients. No process execution."""
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy import func, select

from . import collaboration, repositories, tasks
from . import schemas as S
from .models import AgentDispatch, AgentEndpoint, ProjectProgress
from .security import HubError, now


def own(actor, row):
    return row.user_id == actor.user.id and row.device_id == actor.device.id


def endpoint_view(row):
    return {"id": str(row.id), "project_id": str(row.project_id),
            "repository_id": str(row.repository_id) if row.repository_id else None,
            "user_id": str(row.user_id), "device_id": str(row.device_id), "instance_id": str(row.instance_id),
            "name": row.name, "harness": row.harness, "roles": row.roles, "enabled": row.enabled,
            "role_profiles": row.role_profiles,
            "version": row.version, "last_seen_at": row.last_seen_at.isoformat(),
            "recent_agent_activity": row.last_seen_at > now() - timedelta(seconds=180),
            "automatic_execution": False, "presence_basis": "authenticated-agent-poll-or-registration"}


def get_endpoint(db, project_id, endpoint_id, *, actor=None):
    row = db.scalar(select(AgentEndpoint).where(AgentEndpoint.project_id == project_id, AgentEndpoint.id == endpoint_id))
    if not row or actor and not own(actor, row):
        raise HubError("AGENT_NOT_ACCESSIBLE", 404)
    return row


def require_eligible(db, row):
    if not db.scalar(select(func.hub.agent_endpoint_eligible(row.id))):
        raise HubError("AGENT_NOT_AVAILABLE", 409)


def register(hub, db, actor, project_id, body, request_id):
    project = hub.project(db, actor, project_id, "tasks:write")
    hub.project(db, actor, project_id, "tasks:claim")
    if body.repository_id:
        repositories.require(db, project_id, body.repository_id, "code:write")
    elif project.repository_mode != "legacy":
        raise HubError("AGENT_REPOSITORY_REQUIRED", 422)
    row = db.scalar(select(AgentEndpoint).where(AgentEndpoint.project_id == project_id,
                    AgentEndpoint.device_id == actor.device.id, AgentEndpoint.instance_id == body.instance_id))
    collaboration.endpoint_profiles(db, project_id, body.role_profiles, body.roles)
    values = {"name": body.name, "roles": body.roles, "role_profiles": body.role_profiles, "enabled": body.enabled,
              "grant_id": actor.grant.id if actor.grant else None,
              "user_epoch": actor.user.auth_epoch, "device_epoch": actor.device.epoch}
    if row:
        if not own(actor, row) or row.repository_id != body.repository_id or row.harness != body.harness:
            raise HubError("AGENT_IDENTITY_FROZEN", 409)
        if row.version != body.expected_version:
            raise HubError("AGENT_VERSION_CONFLICT", 409, current_version=row.version)
        if any(getattr(row, key) != value for key, value in values.items()):
            row.version += 1
        for key, value in values.items():
            setattr(row, key, value)
        row.last_seen_at = now()
    else:
        if body.expected_version:
            raise HubError("AGENT_VERSION_CONFLICT", 409)
        count = db.scalar(select(func.count()).select_from(AgentEndpoint).where(
            AgentEndpoint.project_id == project_id, AgentEndpoint.device_id == actor.device.id))
        if count >= 128:
            raise HubError("AGENT_REGISTRATION_LIMIT", 409)
        row = AgentEndpoint(project_id=project_id, repository_id=body.repository_id, user_id=actor.user.id,
                            device_id=actor.device.id, instance_id=body.instance_id, harness=body.harness,
                            version=1, last_seen_at=now(), **values)
        db.add(row)
    db.flush()
    hub.audit(db, actor.user.id, "agent.registered", row.id, request_id, project_id,
              repository_id=row.repository_id, version=row.version, enabled=row.enabled)
    return endpoint_view(row)


def dispatch_view(db, row):
    task = tasks.get_task(db, row.project_id, row.task_id)
    return {"id": str(row.id), "project_id": str(row.project_id), "task_id": str(row.task_id),
            "repository_id": str(task.repository_id) if task.repository_id else None,
            "endpoint_id": str(row.endpoint_id), "sender_id": str(row.sender_id), "state": row.state,
            "version": row.version, "task_version": row.task_version, "endpoint_version": row.endpoint_version,
            "note": row.note, "reason": row.reason, "task_title": task.title, "task_status": task.status,
            "created_at": row.created_at.isoformat(), "expires_at": row.expires_at.isoformat(),
            "expired": row.expires_at <= now(), "received_at": row.received_at.isoformat() if row.received_at else None,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "run_id": str(row.run_id) if row.run_id else None, "automatic_execution": False}


def get_dispatch(db, project_id, dispatch_id):
    row = db.scalar(select(AgentDispatch).where(AgentDispatch.project_id == project_id, AgentDispatch.id == dispatch_id))
    if not row:
        raise HubError("AGENT_DISPATCH_NOT_ACCESSIBLE", 404)
    tasks.get_task(db, project_id, row.task_id)
    return row


def event(hub, db, actor, row, request_id):
    task = tasks.get_task(db, row.project_id, row.task_id)
    labels = {"queued": "已投递待办", "received": "已接收待办", "accepted": "已接受并认领", "declined": "已拒绝待办",
              "cancelled": "已取消待办", "expired": "待办已过期"}
    db.add(ProjectProgress(project_id=row.project_id, repository_id=task.repository_id, author_id=actor.user.id,
        device_id=actor.device.id, status="checkpoint", summary=(labels[row.state] + " · " + task.title)[:300],
        detail="", task_reference=str(task.id), task_id=task.id, run_id=row.run_id, kind="agent.dispatch." + row.state))
    hub.audit(db, actor.user.id, "agent.dispatch." + row.state, row.id, request_id, row.project_id,
              repository_id=task.repository_id, dispatch_version=row.version, endpoint_id=str(row.endpoint_id))


def send(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    task = tasks.get_task(db, project_id, body.task_id)
    endpoint = get_endpoint(db, project_id, body.endpoint_id)
    require_eligible(db, endpoint)
    tasks.check_version(task, body.expected_task_version)
    if endpoint.version != body.expected_endpoint_version:
        raise HubError("AGENT_VERSION_CONFLICT", 409)
    if task.repository_id != endpoint.repository_id:
        raise HubError("AGENT_REPOSITORY_MISMATCH", 422)
    if not collaboration.endpoint_matches(task, endpoint):
        raise HubError("AGENT_ROLE_MISMATCH", 422)
    if task.status != "ready":
        raise HubError("TASK_NOT_CLAIMABLE", 409)
    previous = db.scalar(select(AgentDispatch).where(AgentDispatch.task_id == task.id,
                         AgentDispatch.state.in_(["queued", "received"])))
    if previous:
        if previous.expires_at > now():
            raise HubError("AGENT_DISPATCH_ALREADY_PENDING", 409)
        previous.state, previous.decided_at = "expired", now()
        previous.version += 1
        event(hub, db, actor, previous, request_id)
        db.flush()
    row = AgentDispatch(id=uuid.uuid4(), project_id=project_id, task_id=task.id, endpoint_id=endpoint.id,
        sender_id=actor.user.id, sender_device_id=actor.device.id, task_version=task.version,
        endpoint_version=endpoint.version, note=body.note, state="queued", version=1,
        expires_at=now() + timedelta(seconds=body.expires_in_seconds))
    db.add(row)
    db.flush()
    event(hub, db, actor, row, request_id)
    return dispatch_view(db, row)


def guard_decision(hub, db, actor, project_id, row, action):
    if action == "cancel":
        hub.project(db, actor, project_id, "tasks:publish")
        return None
    hub.project(db, actor, project_id, "tasks:claim")
    hub.project(db, actor, project_id, "tasks:write")
    endpoint = get_endpoint(db, project_id, row.endpoint_id, actor=actor)
    require_eligible(db, endpoint)
    return endpoint


def decide(hub, db, actor, project_id, row, body, request_id):
    endpoint = guard_decision(hub, db, actor, project_id, row, body.action)
    if row.version != body.expected_version:
        raise HubError("AGENT_DISPATCH_VERSION_CONFLICT", 409)
    if row.state not in {"queued", "received"}:
        raise HubError("AGENT_DISPATCH_FINAL", 409)
    if body.action != "cancel" and row.expires_at <= now():
        raise HubError("AGENT_DISPATCH_EXPIRED", 409)
    if body.action in {"receive", "accept"}:
        if endpoint.version != row.endpoint_version:
            raise HubError("AGENT_DISPATCH_STALE", 409)
        task = tasks.get_task(db, project_id, row.task_id)
        tasks.check_version(task, row.task_version)
        if task.repository_id != endpoint.repository_id or not collaboration.endpoint_matches(task, endpoint):
            raise HubError("AGENT_DISPATCH_STALE", 409)
    result = {}
    if body.action == "receive":
        if row.state == "received":
            return {"dispatch": dispatch_view(db, row)}
        row.state, row.received_at = "received", now()
    elif body.action == "accept":
        result = tasks.claim(hub, db, actor, project_id, row.task_id,
                             S.TaskClaim(instance_id=endpoint.instance_id, harness=endpoint.harness), request_id)
        row.state, row.run_id = "accepted", uuid.UUID(result["run"]["id"])
        row.received_at = row.received_at or now()
        row.decided_at = now()
    else:
        row.state = "cancelled" if body.action == "cancel" else "declined"
        row.decided_at, row.reason = now(), body.reason.strip()
    row.version += 1
    db.flush()
    event(hub, db, actor, row, request_id)
    return {"dispatch": dispatch_view(db, row), **result}


def install(app, hub, actor, mutate, dependency):
    DB = Annotated[object, Depends(dependency, scope="function")]

    def device_mutation(request, db, identity, body, action):
        # Recipient identity includes the device; an account's other device must
        # never replay a registration or claim receipt for this device.
        return hub.idempotent(db, identity, f"{request.method}:{request.url.path}:{identity.device.id}",
                              request.headers.get("Idempotency-Key"), body.model_dump(mode="python"), action)

    @app.get("/api/projects/{project_id}/agents")
    def agents(project_id: uuid.UUID, request: Request, db: DB, repository_id: uuid.UUID | None = None,
               offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        statement = select(AgentEndpoint).where(AgentEndpoint.project_id == project_id)
        if repository_id:
            statement = statement.where(AgentEndpoint.repository_id == repository_id)
        rows = list(db.scalars(statement.order_by(AgentEndpoint.created_at, AgentEndpoint.id).offset(offset).limit(limit)))
        return {"items": [endpoint_view(row) for row in rows], "has_more": len(rows) == limit, "next_offset": offset + len(rows)}

    @app.post("/api/projects/{project_id}/agents")
    def registration(project_id: uuid.UUID, body: S.AgentRegistration, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        hub.project(db, identity, project_id, "tasks:claim")
        if body.repository_id:
            repositories.require(db, project_id, body.repository_id, "code:write")
        return device_mutation(request, db, identity, body, lambda: register(hub, db, identity, project_id, body, request.state.request_id))

    @app.post("/api/projects/{project_id}/agents/{endpoint_id}/heartbeat")
    def heartbeat(project_id: uuid.UUID, endpoint_id: uuid.UUID, body: S.AgentHeartbeat, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        row = get_endpoint(db, project_id, endpoint_id, actor=identity)
        require_eligible(db, row)
        def update():
            if row.version != body.expected_version:
                raise HubError("AGENT_VERSION_CONFLICT", 409)
            row.last_seen_at = now()
            return endpoint_view(row)
        return device_mutation(request, db, identity, body, update)

    @app.get("/api/projects/{project_id}/agent-dispatches")
    def inbox(project_id: uuid.UUID, request: Request, db: DB, endpoint_id: uuid.UUID | None = None,
              repository_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
              offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        statement = select(AgentDispatch).join(tasks.Task, tasks.Task.id == AgentDispatch.task_id).where(AgentDispatch.project_id == project_id)
        if endpoint_id:
            get_endpoint(db, project_id, endpoint_id)
            statement = statement.where(AgentDispatch.endpoint_id == endpoint_id)
        if repository_id:
            statement = statement.where(tasks.Task.repository_id == repository_id)
        if task_id:
            tasks.get_task(db, project_id, task_id)
            statement = statement.where(AgentDispatch.task_id == task_id)
        rows = list(db.scalars(statement.order_by(AgentDispatch.created_at.desc(), AgentDispatch.id).offset(offset).limit(limit)))
        return {"items": [dispatch_view(db, row) for row in rows], "has_more": len(rows) == limit, "next_offset": offset + len(rows)}

    @app.post("/api/projects/{project_id}/agent-dispatches")
    def dispatch(project_id: uuid.UUID, body: S.AgentDispatchInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        tasks.get_task(db, project_id, body.task_id)
        row = get_endpoint(db, project_id, body.endpoint_id)
        require_eligible(db, row)
        return device_mutation(request, db, identity, body, lambda: send(hub, db, identity, project_id, body, request.state.request_id))

    @app.get("/api/projects/{project_id}/agent-dispatches/{dispatch_id}")
    def read_dispatch(project_id: uuid.UUID, dispatch_id: uuid.UUID, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        return dispatch_view(db, get_dispatch(db, project_id, dispatch_id))

    @app.post("/api/projects/{project_id}/agent-dispatches/{dispatch_id}/decision")
    def decision(project_id: uuid.UUID, dispatch_id: uuid.UUID, body: S.AgentDispatchDecision, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id)
        row = get_dispatch(db, project_id, dispatch_id)
        guard_decision(hub, db, identity, project_id, row, body.action)
        return device_mutation(request, db, identity, body, lambda: decide(hub, db, identity, project_id, row, body, request.state.request_id))
