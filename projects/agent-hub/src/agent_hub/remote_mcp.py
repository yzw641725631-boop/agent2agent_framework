"""Stateless Streamable HTTP MCP for scoped cloud reading and progress coordination.

Local Git observation, hooks, worktrees and uninstall stay with the authenticated
desktop bridge; a remote Bearer token cannot attest those local operations.
"""

import json
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, documents, memory, oauth, progress, repositories, tasks
from . import schemas as S
from .security import HubError, rate_limit

PROTOCOLS = {"2025-03-26", "2025-06-18", "2025-11-25"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


class Page(S.Input):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=20, ge=1, le=50)


class TaskInput(S.Input):
    task_id: uuid.UUID


class LogPage(TaskInput, Page):
    pass


class LogRead(TaskInput):
    worklog_id: uuid.UUID


class DocumentRead(S.DocumentRead):
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=6000, ge=100, le=12000)


class MemorySearch(S.Input):
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=6, ge=1, le=8)


class ProgressReport(S.ProgressReport):
    operation_id: uuid.UUID


TOOLS = {
    "hub_get_project": (S.Empty, "读取此连接固定的项目和仓库，以及可用操作范围。"),
    "hub_list_tasks": (Page, "分页读取此仓库的任务状态及工作范围。"),
    "hub_get_task": (TaskInput, "读取此仓库中一个任务的当前版本。"),
    "hub_list_worklogs": (LogPage, "分页读取当前任务共享工作记录的索引，按需展开正文。"),
    "hub_read_worklog": (LogRead, "读取一个获准的工作记录，保留来源和核验信息。"),
    "hub_search_documents": (S.DocumentSearch, "在本仓库获准文档中进行关键词检索。"),
    "hub_read_document": (DocumentRead, "分段读取一篇已索引文档，按返回的偏移继续。"),
    "hub_search_memory": (MemorySearch, "有界关键词检索本仓库共享记忆；结果仍须核对原始来源。"),
    "hub_report_progress": (ProgressReport, "保存仓库进展通告；不改变任务正式状态或证明代码已经交付。"),
}


def dispatch(hub, db, identity, client, name, body, request_id):
    project_id, repository_id = client.project_id, client.repository_id
    hub.project(db, identity, project_id)
    repository = repositories.require(db, project_id, repository_id)
    if hasattr(body, "task_id"):
        task = tasks.get_task(db, project_id, body.task_id)
        if task.repository_id != repository_id:
            raise HubError("TASK_NOT_FOUND", 404)
    if name == "hub_get_project":
        project = hub.project(db, identity, project_id)
        return {
            "project_id": str(project_id),
            "name": project.name,
            "repository": repositories.view(db, repository),
            "local_operations": "Use the authenticated Agent Hub desktop MCP for code observation, "
            "task execution, Git delivery and revocation closeout.",
        }
    if name == "hub_list_tasks":
        return tasks.page(db, project_id, body.offset, body.limit, repository_id=repository_id)
    if name == "hub_get_task":
        return {"task": tasks.task_view(task)}
    if name in {"hub_list_worklogs", "hub_read_worklog"}:
        hub.project(db, identity, project_id, "logs:read")
        if name == "hub_list_worklogs":
            return tasks.log_index(db, project_id, task.id, body.offset, body.limit)
        return tasks.read_log(db, project_id, task.id, body.worklog_id)
    if name in {"hub_search_documents", "hub_read_document"}:
        repositories.source_access(hub, db, identity, project_id, repository_id)
        if name == "hub_search_documents":
            return documents.search(db, project_id, body, repository_id=repository_id)
        result = documents.read(db, project_id, body, repository_id=repository_id)
        content = result.pop("content")
        result.update(
            content=content[body.offset : body.offset + body.limit],
            offset=body.offset,
            next_offset=body.offset + len(content[body.offset : body.offset + body.limit]),
            has_more=body.offset + body.limit < len(content),
        )
        return result
    if name == "hub_search_memory":
        return memory.search(
            db,
            project_id,
            S.MemorySearch(
                query=body.query, limit=body.limit, repository_id=repository_id, search_mode="keyword"
            ),
        )
    if name == "hub_report_progress":
        if body.repository_id and body.repository_id != repository_id:
            raise HubError("REPOSITORY_NOT_ACCESSIBLE", 404)
        value = S.ProgressReport(
            **(body.model_dump(exclude={"operation_id", "repository_id"}) | {"repository_id": repository_id})
        )
        hub.project(db, identity, project_id, "tasks:write")
        return hub.idempotent(
            db,
            identity,
            "remote-mcp-progress:" + str(repository_id),
            str(body.operation_id),
            value.model_dump(),
            lambda: progress.report(hub, db, identity, project_id, value, request_id),
        )
    raise HubError("MCP_TOOL_NOT_AVAILABLE", 404)


def install(app, hub, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]
    origin = hub.settings.public_url.rstrip("/")
    endpoint = "/mcp/projects/{project_id}/repositories/{repository_id}"
    metadata_path = "/.well-known/oauth-protected-resource" + endpoint

    def audience(project_id, repository_id):
        return origin + f"/mcp/projects/{project_id}/repositories/{repository_id}"

    def origin_valid(request, db):
        return oauth.origin_allowed(hub, db, request.headers.get("origin"))

    def cors(request):
        return (
            {
                "Access-Control-Allow-Origin": request.headers["origin"],
                "Vary": "Origin",
                "Access-Control-Expose-Headers": "WWW-Authenticate, MCP-Protocol-Version",
            }
            if request.headers.get("origin")
            else {}
        )

    @app.get(metadata_path)
    def protected_metadata(project_id: uuid.UUID, repository_id: uuid.UUID):
        return JSONResponse(
            {
                "resource": audience(project_id, repository_id),
                "authorization_servers": [origin],
                "scopes_supported": sorted(oauth.SCOPES),
                "bearer_methods_supported": ["header"],
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.options(endpoint)
    async def preflight(project_id: uuid.UUID, repository_id: uuid.UUID, request: Request, db: DB):
        if not await run_in_threadpool(origin_valid, request, db):
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers=cors(request)
            | {
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version",
                "Access-Control-Max-Age": "600",
            },
        )

    @app.api_route(endpoint, methods=["GET", "POST", "DELETE"])
    async def mcp(project_id: uuid.UUID, repository_id: uuid.UUID, request: Request, db: DB):
        if not await run_in_threadpool(origin_valid, request, db):
            return Response(status_code=403)
        headers = cors(request)
        resource = audience(project_id, repository_id)
        try:
            if request.query_params or not request.headers.get("Authorization", "").startswith("Bearer "):
                raise oauth.Failure("invalid_token", 401)
            identity, client, session = await run_in_threadpool(
                oauth.authenticate, hub, db, request.headers["Authorization"][7:], resource
            )
        except oauth.Failure:
            location = origin + "/.well-known/oauth-protected-resource" + request.url.path
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers=headers
                | {"WWW-Authenticate": f'Bearer resource_metadata="{location}", scope="mcp:read"'},
            )
        await run_in_threadpool(
            rate_limit, security_sessions, hub.secret, "remote-mcp:" + str(client.id), 300, 60
        )
        request.state.actor_id = identity.user.id
        if "mcp:read" not in session.scopes:
            return Response(
                status_code=403,
                headers=headers | {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="mcp:read"'},
            )
        sent = request.headers.get("origin")
        if (
            sent
            and sent != origin
            and not any(
                sent == urlsplit(uri).scheme + "://" + urlsplit(uri).netloc for uri in client.redirect_uris
            )
        ):
            return Response(status_code=403)
        if request.method != "POST":
            return Response(status_code=405, headers=headers | {"Allow": "POST, OPTIONS"})
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return Response(status_code=415, headers=headers)
        accepted = request.headers.get("accept", "")
        if "application/json" not in accepted or "text/event-stream" not in accepted:
            return Response(status_code=406, headers=headers)
        data = bytearray()
        async for block in request.stream():
            data.extend(block)
            if len(data) > 65536:
                return Response(status_code=413, headers=headers)
        try:
            message = json.loads(data, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
                headers=headers,
            )
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and (type(message["id"]) not in {str, int} or len(str(message["id"])) > 200))
        ):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}},
                status_code=400,
                headers=headers,
            )
        method, params = message["method"], message.get("params", {})
        protocol = request.headers.get("MCP-Protocol-Version", "2025-03-26")
        if protocol not in PROTOCOLS:
            return Response(status_code=400, headers=headers)
        if not isinstance(params, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32602, "message": "Invalid params"},
                },
                status_code=400,
                headers=headers,
            )
        if "id" not in message:
            if method not in {"notifications/initialized", "notifications/cancelled"}:
                return Response(status_code=400, headers=headers)
            return Response(status_code=202, headers=headers)
        result = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize":
            if (
                not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("clientInfo"), dict)
                or not isinstance(params["clientInfo"].get("name"), str)
                or not isinstance(params["clientInfo"].get("version"), str)
            ):
                return JSONResponse(
                    {**result, "error": {"code": -32602, "message": "Invalid params"}},
                    status_code=400,
                    headers=headers,
                )
            result["result"] = {
                "protocolVersion": params["protocolVersion"]
                if params["protocolVersion"] in PROTOCOLS
                else "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-hub-remote", "version": __version__},
                "instructions": "This connection is fixed to one approved repository. Use tools/list. "
                "Local code observation and execution require the authenticated desktop bridge.",
            }
        elif method == "ping":
            result["result"] = {}
        elif method == "tools/list":
            result["result"] = {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": model.model_json_schema(),
                        "annotations": {
                            "readOnlyHint": name != "hub_report_progress",
                            "destructiveHint": False,
                        },
                    }
                    for name, (model, description) in TOOLS.items()
                    if name != "hub_report_progress" or "mcp:coordinate" in session.scopes
                ]
            }
        elif method == "tools/call":
            try:
                if not isinstance(params, dict) or params.get("name") not in TOOLS:
                    raise HubError("MCP_TOOL_NOT_AVAILABLE", 404)
                name = params["name"]
                if name == "hub_report_progress" and "mcp:coordinate" not in session.scopes:
                    raise HubError("OAUTH_SCOPE_REQUIRED", 403)
                body = TOOLS[name][0].model_validate(params.get("arguments", {}))

                # Errors are returned as tool results, so roll back the tool's writes
                # explicitly; returning isError must never commit a partial operation.
                def call_tool():
                    with db.begin_nested():
                        value = dispatch(hub, db, identity, client, name, body, request.state.request_id)
                        encoded = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
                        if len(encoded.encode()) > 60000:
                            raise HubError("MCP_RESPONSE_BUDGET_EXCEEDED", 409)
                        return encoded

                encoded = await run_in_threadpool(call_tool)
                result["result"] = {"content": [{"type": "text", "text": encoded}], "isError": False}
            except (HubError, ValidationError) as error:
                code = error.code if isinstance(error, HubError) else "INVALID_REQUEST"
                result["result"] = {
                    "content": [{"type": "text", "text": json.dumps({"code": code})}],
                    "isError": True,
                }
        else:
            result["error"] = {"code": -32601, "message": "Method not found"}
        return JSONResponse(result, headers=headers)
