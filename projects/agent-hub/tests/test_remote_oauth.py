"""Exercise real authorization-code, token rotation and scoped remote MCP over HTTP."""

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from test_repositories import grant, register, task

from agent_hub.models import Device, Idempotency
from agent_hub.oauth import Session
from agent_hub.security import b64

CALLBACK = "https://tool.example/callback"
VERIFIER = "v" * 64


def setup(w, *, member=False, coordinate=False):
    p = w.project()
    repo = register(w, p)
    who = w.root
    gid = None
    if member:
        who = w.member(p)
        gid = grant(w, who, p, [(repo, ["code:read", "documents:read"])])
        w.start(who, gid)
    body = {
        "name": "Approved cloud reader",
        "user_id": who.user_id,
        "device_id": who.enrollment["device_id"],
        "grant_id": gid,
        "project_id": p,
        "repository_id": repo["id"],
        "redirect_uris": [CALLBACK],
        "scopes": ["mcp:read", "mcp:coordinate"] if coordinate else ["mcp:read"],
        "reason": "Explicitly approve this known device for the fixed repository.",
    }
    client = w.ok("POST", "/api/admin/oauth/clients", who=w.root, body=body)
    return p, repo, who, client, body


def start(w, client, **overrides):
    return w.client.get(
        "/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "resource": client["resource"],
            "scope": " ".join(client["scopes"]),
            "state": "client-csrf-state",
            "code_challenge": b64(hashlib.sha256(VERIFIER.encode()).digest()),
            "code_challenge_method": "S256",
            **overrides,
        },
        follow_redirects=False,
    )


def approve(w, client, who):
    response = start(w, client)
    assert response.status_code == 302, response.text
    page = w.client.get(response.headers["location"])
    assert page.status_code == 200 and 'id="app"' in page.text
    rid = parse_qs(urlsplit(response.headers["location"]).query)["oauth_request"][0]
    pending = w.ok("GET", f"/api/me/oauth/requests/{rid}", who=who)
    assert pending["resource"] == client["resource"]
    consent = w.ok("POST", f"/api/me/oauth/requests/{rid}", who=who, body={"approve": True})
    query = parse_qs(urlsplit(consent["redirect_uri"]).query)
    assert query["state"] == ["client-csrf-state"] and query["iss"] == ["http://testserver"]
    return query["code"][0]


def exchange(w, client, code, **overrides):
    return w.client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "resource": client["resource"],
            "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER,
            "code": code,
            **overrides,
        },
    )


def token(w, client, who):
    response = exchange(w, client, approve(w, client, who))
    assert response.status_code == 200, response.text
    return response.json()


def remote(w, client, tokens, method="tools/list", params=None, **headers):
    return w.client.post(
        urlsplit(client["resource"]).path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        },
        headers={
            "Authorization": "Bearer " + tokens["access_token"],
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )


def test_pkce_scope_audience_and_single_use_codes(world):
    w = world
    _, _, who, client, _ = setup(w)
    assert start(w, client, redirect_uri=CALLBACK + "/wrong").status_code == 400
    assert start(w, client, code_challenge_method="plain").status_code == 400
    assert start(w, client, scope="mcp:coordinate").status_code == 400
    code = approve(w, client, who)
    assert exchange(w, client, code, code_verifier="w" * 64).status_code == 400
    assert exchange(w, client, code, resource=client["resource"] + "/other").status_code == 400
    response = exchange(w, client, code)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert exchange(w, client, code).status_code == 400
    tokens = response.json()
    assert "hub_report_progress" not in [
        t["name"] for t in remote(w, client, tokens).json()["result"]["tools"]
    ]
    with w.owner() as db:
        row = db.scalar(select(Session))
        assert tokens["access_token"] not in (row.access_digest, row.refresh_digest)
        cached = json.dumps([r.result for r in db.scalars(select(Idempotency))], default=str)
        assert tokens["access_token"] not in cached and code not in cached


def test_refresh_rotation_replay_revokes_family_and_normal_source_refresh_works(world):
    w = world
    _, _, who, client, _ = setup(w)
    tokens = token(w, client, who)
    who.tokens = w.ok("POST", "/api/auth/refresh", key=who.key, token=who.tokens["refresh_token"], body={})
    assert remote(w, client, tokens).status_code == 200
    data = {
        "grant_type": "refresh_token",
        "client_id": client["client_id"],
        "resource": client["resource"],
        "refresh_token": tokens["refresh_token"],
    }
    current = w.client.post("/oauth/token", data=data)
    assert current.status_code == 200, current.text
    assert remote(w, client, tokens).status_code == 401
    assert remote(w, client, current.json()).status_code == 200
    assert w.client.post("/oauth/token", data=data).status_code == 400
    assert remote(w, client, current.json()).status_code == 401


def test_fixed_repository_and_read_only_tokens_cannot_coordinate(world):
    w = world
    p, repo, who, client, _ = setup(w, member=True)
    private = register(w, p, "pc")
    visible_task = task(w, p, repo)
    private_task = task(w, p, private)
    tokens = token(w, client, who)
    result = remote(w, client, tokens, "tools/call", {"name": "hub_list_tasks", "arguments": {}}).json()
    items = json.loads(result["result"]["content"][0]["text"])["items"]
    assert [t["id"] for t in items] == [visible_task["id"]]
    hidden = remote(
        w,
        client,
        tokens,
        "tools/call",
        {"name": "hub_get_task", "arguments": {"task_id": private_task["id"]}},
    ).json()
    assert hidden["result"]["isError"]
    denied = remote(w, client, tokens, "tools/call", {"name": "hub_report_progress", "arguments": {}})
    assert denied.json()["result"]["isError"]
    other = {**client, "resource": client["resource"].replace(repo["id"], private["id"])}
    assert remote(w, other, tokens).status_code == 401


def test_device_and_client_revocation_stop_access_and_refresh(world):
    w = world
    _, _, who, client, _ = setup(w, member=True)
    tokens = token(w, client, who)
    with w.owner.begin() as db:
        db.get(Device, uuid.UUID(who.enrollment["device_id"])).status = "revoked"
    assert remote(w, client, tokens).status_code == 401
    refresh = w.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client["client_id"],
            "resource": client["resource"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 400
    w.ok("POST", f"/api/admin/oauth/clients/{client['id']}/revoke", who=w.root, body={})
    assert remote(w, client, tokens).status_code == 401


def test_consent_cannot_be_given_by_another_user_and_can_be_denied(world):
    w = world
    _, _, who, client, _ = setup(w, member=True)
    response = start(w, client)
    rid = parse_qs(urlsplit(response.headers["location"]).query)["oauth_request"][0]
    assert (
        w.call("POST", f"/api/me/oauth/requests/{rid}", who=w.root, body={"approve": True}).status_code == 403
    )
    denied = w.ok("POST", f"/api/me/oauth/requests/{rid}", who=who, body={"approve": False})
    assert parse_qs(urlsplit(denied["redirect_uri"]).query)["error"] == ["access_denied"]
    assert w.call("GET", f"/api/me/oauth/requests/{rid}", who=who).status_code == 403


def test_concurrent_exchange_only_issues_one_session(world):
    w = world
    _, _, who, client, _ = setup(w)
    code = approve(w, client, who)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: exchange(w, client, code), range(2)))
    assert sorted(r.status_code for r in replies) == [200, 400]


def test_discovery_cors_and_streamable_http_framing(world):
    w = world
    _, _, who, client, _ = setup(w)
    tokens = token(w, client, who)
    path = urlsplit(client["resource"]).path
    assert w.client.get(path).status_code == 401
    assert "resource_metadata=" in w.client.get(path).headers["www-authenticate"]
    assert (
        w.client.get("/.well-known/oauth-protected-resource" + path).json()["resource"] == client["resource"]
    )
    discovery = w.client.get("/.well-known/oauth-authorization-server")
    assert discovery.json()["code_challenge_methods_supported"] == ["S256"]
    assert discovery.headers["access-control-allow-origin"] == "*"
    assert remote(w, client, tokens, Origin="https://evil.example").status_code == 403
    assert (
        remote(w, client, tokens, Origin="https://tool.example").headers["access-control-allow-origin"]
        == "https://tool.example"
    )
    options = w.client.options(
        "/oauth/token", headers={"Origin": "https://tool.example", "Access-Control-Request-Method": "POST"}
    )
    assert (
        options.status_code == 200
        and options.headers["access-control-allow-origin"] == "https://tool.example"
    )
    assert w.client.options("/oauth/token", headers={"Origin": "https://evil.example"}).status_code == 403
    assert remote(w, client, tokens, Accept="application/json").status_code == 406
    assert remote(w, client, tokens, **{"MCP-Protocol-Version": "unsupported"}).status_code == 400
    init = remote(
        w,
        client,
        tokens,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "integration-test", "version": "1"},
        },
    )
    assert init.json()["result"]["serverInfo"]["name"] == "agent-hub-remote"
    assert (
        w.client.get(path, headers={"Authorization": "Bearer " + tokens["access_token"]}).status_code == 405
    )
    assert (
        w.client.post(
            path + "?token=secret", json={}, headers={"Authorization": "Bearer " + tokens["access_token"]}
        ).status_code
        == 401
    )


def test_member_cannot_register_and_owner_can_revoke_own_oauth_family(world):
    w = world
    _, _, who, client, body = setup(w, member=True)
    assert w.call("POST", "/api/admin/oauth/clients", who=who, body=body).status_code == 403
    tokens = token(w, client, who)
    own = w.ok("GET", "/api/me/oauth/sessions", who=who)["items"]
    assert len(own) == 1 and "access_token" not in json.dumps(own)
    w.ok("POST", f"/api/me/oauth/sessions/{own[0]['id']}/revoke", who=who, body={})
    assert remote(w, client, tokens).status_code == 401


def test_tool_error_rolls_back_partial_writes(world, monkeypatch):
    from agent_hub import remote_mcp
    from agent_hub.models import ProjectProgress
    from agent_hub.security import HubError

    w = world
    p, repo, who, client, _ = setup(w, coordinate=True)
    tokens = token(w, client, who)
    original = remote_mcp.dispatch

    def fail(*args):
        original(*args)
        raise HubError("SIMULATED_AFTER_WRITE", 409)

    monkeypatch.setattr(remote_mcp, "dispatch", fail)
    result = remote(
        w,
        client,
        tokens,
        "tools/call",
        {
            "name": "hub_report_progress",
            "arguments": {
                "summary": "Record progress without changing task state.",
                "status": "checkpoint",
                "operation_id": str(uuid.uuid4()),
                "repository_id": repo["id"],
            },
        },
    )
    assert result.json()["result"]["isError"]
    assert "SIMULATED_AFTER_WRITE" in result.json()["result"]["content"][0]["text"]
    with w.owner() as db:
        assert not list(db.scalars(select(ProjectProgress).where(ProjectProgress.project_id == uuid.UUID(p))))


@pytest.mark.parametrize(
    "uri",
    ["http://external.example/cb", "https://user:pass@tool.example/cb", "https://tool.example/cb#fragment"],
)
def test_registration_rejects_unsafe_redirects(world, uri):
    _, _, _, _, body = setup(world)
    assert (
        world.call(
            "POST", "/api/admin/oauth/clients", who=world.root, body={**body, "redirect_uris": [uri]}
        ).status_code
        == 422
    )
