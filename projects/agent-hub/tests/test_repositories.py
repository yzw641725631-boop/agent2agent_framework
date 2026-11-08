import base64
import json
import uuid

import httpx
import pytest
from sqlalchemy import select
from test_documents import CONFIG, TOKEN, fixture_source, indexer  # noqa: F401
from test_tasks import claim, lease, log_body, ready

from agent_hub.db import context, session_factory
from agent_hub.git_gateway import branch_for, validate_receive_commands
from agent_hub.models import ProjectRepository, RepositoryTicket, Task, Worklog
from agent_hub.security import HubError


def register(w, project, slug="web", **overrides):
    return w.ok(
        "POST",
        f"/api/projects/{project}/repositories",
        who=w.root,
        body={
            "slug": slug,
            "name": slug.upper(),
            "platform": slug if slug in {"web", "pc", "api"} else "shared",
            "repository_url": f"https://github.com/example/{slug}",
            "branch": "main",
            "auth_mode": "token",
            "token": TOKEN,
            **overrides,
        },
    )


def grant(w, member, project, permissions, caps=None, *, approve=True, requester=None):
    approval = w.ok(
        "POST",
        "/api/grant-requests",
        who=requester or w.root,
        body={
            "project_id": project,
            "user_id": member.user_id,
            "device_id": member.enrollment["device_id"],
            "capabilities": caps
            or ["project:read", "tasks:claim", "tasks:write", "documents:read", "logs:read"],
            "repository_permissions": [
                {"repository_id": repo["id"], "capabilities": capabilities}
                for repo, capabilities in permissions
            ],
        },
    )
    return w.decide(approval)["grant_id"] if approve else approval


def task(w, project, repo, **overrides):
    return ready(
        w,
        project,
        repository_id=repo["id"],
        scope={"repository": repo["slug"], "platform": repo["platform"], "paths": ["src", "tests"]},
        **overrides,
    )


def basic(secret):
    return "Basic " + base64.b64encode(("agent-hub:" + secret).encode()).decode()


def test_repository_approval_code_write_and_inherited_record_rls(world, database):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write", "documents:read"])]))
    visible = w.ok("GET", f"/api/projects/{p}/repositories", who=member)
    assert [r["id"] for r in visible["items"]] == [web["id"]]
    assert visible["repository_mode"] == "scoped"
    assert TOKEN not in json.dumps(visible) and "auth_mode" not in visible["items"][0]
    wt, pt = task(w, p, web), task(w, p, pc, title="private-pc-task")
    pc_run = claim(w, p, pt)
    w.ok(
        "POST",
        f"/api/projects/{p}/tasks/{pt['id']}/worklogs",
        who=w.root,
        body=log_body(pc_run, notes="private-pc-code", git_commit="a" * 40),
    )
    assert [t["id"] for t in w.ok("GET", f"/api/projects/{p}/tasks", who=member)["items"]] == [wt["id"]]
    assert w.call("GET", f"/api/projects/{p}/tasks/{pt['id']}", who=member).status_code == 404
    assert w.call("GET", f"/api/projects/{p}/tasks/{pt['id']}/worklogs", who=member).status_code == 404
    feed = w.ok("GET", f"/api/projects/{p}/activity", who=member)
    assert "private-pc" not in json.dumps(feed) and all(
        e["repository_id"] == web["id"] for e in feed["items"]
    )
    assert claim(w, p, wt, member)["run"]["user_id"] == member.user_id
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
            assert [str(r.id) for r in db.scalars(select(ProjectRepository))] == [web["id"]]
            assert [str(t.id) for t in db.scalars(select(Task))] == [wt["id"]]
            assert not list(db.scalars(select(Worklog)))
    finally:
        runtime.kw["bind"].dispose()


def test_project_grant_does_not_implicitly_grant_repositories_and_read_does_not_write(world):
    w, p = world, world.project()
    web = register(w, p)
    t = task(w, p, web)
    member = w.member(p)
    w.start(member, w.grant(member, p, ["project:read", "tasks:claim", "tasks:write", "logs:read"]))
    assert w.ok("GET", f"/api/projects/{p}/repositories", who=member)["items"] == []
    assert w.ok("GET", f"/api/projects/{p}/tasks", who=member)["items"] == []
    w.start(member, grant(w, member, p, [(web, ["code:read"])]))
    assert (
        w.call(
            "POST",
            f"/api/projects/{p}/tasks/{t['id']}/claim",
            who=member,
            body={"instance_id": str(uuid.uuid4()), "harness": "human"},
        ).status_code
        == 404
    )
    assert (
        w.call(
            "POST",
            f"/api/projects/{p}/repositories/{web['id']}/git-ticket",
            who=member,
            body={
                "action": "write",
                "task_id": t["id"],
                "run_id": str(uuid.uuid4()),
                "lease_epoch": 1,
                "expected_commit": "a" * 40,
            },
        ).status_code
        == 404
    )


def test_permission_approvals_are_explicit_project_bound_and_revalidate_archival(world):
    w, p = world, world.project()
    other = w.project("other-project")
    web, alien = register(w, p), register(w, other)
    member = w.member(p)
    response = w.call(
        "POST",
        "/api/grant-requests",
        who=w.root,
        body={
            "project_id": p,
            "user_id": member.user_id,
            "device_id": member.enrollment["device_id"],
            "capabilities": ["project:read"],
            "repository_permissions": [{"repository_id": alien["id"], "capabilities": ["code:read"]}],
        },
    )
    assert response.status_code == 404
    approval = grant(w, member, p, [(web, ["code:read"])], approve=False)
    assert approval["payload"]["repository_permissions"][0]["repository_id"] == web["id"]
    w.ok(
        "POST",
        f"/api/projects/{p}/repositories/{web['id']}/status",
        who=w.root,
        body={"status": "archived", "expected_version": 1},
    )
    response = w.call(
        "POST",
        f"/api/admin/approvals/{approval['id']}/decision",
        who=w.root,
        body={
            "approve": True,
            "expected_version": approval["version"],
            "expected_hash": approval["payload_hash"],
        },
    )
    assert response.status_code == 404


def test_project_administrator_can_request_but_never_approve_repository_access(world):
    w, p = world, world.project()
    web = register(w, p)
    admin = w.administrator(p)
    member = w.member(p)
    approval = grant(w, member, p, [(web, ["code:read", "code:write"])], approve=False, requester=admin)
    decision = {
        "approve": True,
        "expected_version": approval["version"],
        "expected_hash": approval["payload_hash"],
    }
    assert (
        w.call(
            "POST", f"/api/admin/approvals/{approval['id']}/decision", who=admin, body=decision
        ).status_code
        == 403
    )
    assert w.call("GET", f"/api/projects/{p}/repositories", who=member).status_code == 404
    w.start(member, w.decide(approval)["grant_id"])
    assert w.ok("GET", f"/api/projects/{p}/repositories", who=member)["items"][0]["id"] == web["id"]


def test_legacy_records_become_private_when_repository_scope_is_activated(world):
    w, p = world, world.project()
    legacy = ready(w, p)
    member = w.member(p)
    w.start(member, w.grant(member, p))
    assert w.ok("GET", f"/api/projects/{p}/tasks", who=member)["items"]
    register(w, p)
    assert w.ok("GET", f"/api/projects/{p}/tasks", who=member)["items"] == []
    assert w.ok("GET", f"/api/projects/{p}/tasks/{legacy['id']}", who=w.root)["task"]["id"] == legacy["id"]
    assert (
        w.call(
            "POST",
            f"/api/projects/{p}/tasks",
            who=w.root,
            body={
                "title": "ambiguous",
                "description": "ambiguous",
                "acceptance": "ambiguous",
                "scope": {"repository": "web", "platform": "web", "paths": ["src"]},
            },
        ).json()["code"]
        == "TASK_REPOSITORY_REQUIRED"
    )


def test_multiple_document_sources_never_overwrite_or_expose_other_repositories(world, database, indexer):  # noqa: F811
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    for repo in (web, pc):
        base = f"/api/projects/{p}/repositories/{repo['id']}/documents"
        config = {**CONFIG, "repository_url": repo["repository_url"]}
        config.pop("token")
        w.ok("PUT", base + "/repository", who=w.root, body=config)
        w.ok("POST", base + "/syncs", who=w.root, body={})
    worker = indexer(fixture_source(database[0]))
    assert worker.run_once() and worker.run_once()
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["documents:read"])]))
    sources = w.ok("GET", f"/api/projects/{p}/documents/sources", who=member)
    assert [s["repository_id"] for s in sources["items"]] == [web["id"]]
    base = f"/api/projects/{p}/repositories/{web['id']}/documents"
    results = w.ok("POST", base + "/search", who=member, body={})
    assert len(results["items"]) == 2 and results["items"][0]["repository_id"] == web["id"]
    assert (
        w.call(
            "POST", f"/api/projects/{p}/repositories/{pc['id']}/documents/search", who=member, body={}
        ).status_code
        == 404
    )
    assert (
        w.call(
            "POST", f"/api/projects/{p}/repositories/{web['id']}/git-ticket", who=member, body={}
        ).status_code
        == 404
    )


def test_git_ticket_checks_revocation_and_never_exposes_upstream_secret(world):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read"])]))
    ticket = w.ok("POST", f"/api/projects/{p}/repositories/{web['id']}/git-ticket", who=member, body={})
    assert TOKEN not in json.dumps(ticket)
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.host == "github.com" and request.url.path == "/example/web.git/info/refs"
        assert (
            request.headers["authorization"]
            == "Basic " + base64.b64encode(("x-access-token:" + TOKEN).encode()).decode()
        )
        return httpx.Response(
            200, headers={"content-type": "application/x-git-upload-pack-advertisement"}, content=b"0000"
        )

    w.client.app.state.git_transport = httpx.MockTransport(upstream)
    path = ticket["git_path"] + "/info/refs?service=git-upload-pack"
    assert w.client.get(path, headers={"Authorization": basic(ticket["ticket"])}).content == b"0000"
    assert (
        w.client.get(
            path.replace(web["id"], pc["id"]), headers={"Authorization": basic(ticket["ticket"])}
        ).status_code
        == 401
    )
    w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
    assert w.client.get(path, headers={"Authorization": basic(ticket["ticket"])}).status_code in {403, 404}
    assert len(seen) == 1


def test_git_write_ticket_binds_latest_recorded_commit_task_and_ref(world, database):
    w, p = world, world.project()
    web = register(w, p)
    t = task(w, p, web)
    run = claim(w, p, t)
    path = f"/api/projects/{p}/repositories/{web['id']}/git-ticket"
    body = {"action": "write", "task_id": t["id"], **lease(run), "expected_commit": "a" * 40}
    assert w.call("POST", path, who=w.root, body=body).json()["code"] == "GIT_RECORDED_CHECKPOINT_REQUIRED"
    w.ok(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/worklogs",
        who=w.root,
        body=log_body(run, git_commit="a" * 40),
    )
    ticket = w.ok("POST", path, who=w.root, body=body)
    ref = branch_for(t["id"], run["run"]["id"])
    assert ticket["write_ref"] == ref

    def packet(ref, sha="a" * 40):
        line = ("0" * 40 + " " + sha + " " + ref).encode() + b"\x00report-status\n"
        return f"{len(line) + 4:04x}".encode() + line + b"0000PACK"

    validate_receive_commands(packet(ref), ref, "a" * 40)
    for invalid in (
        packet("refs/heads/main"),
        packet(ref, "b" * 40),
        b"0000",
        packet(ref).replace(b"0000PACK", packet(ref)),
    ):
        with pytest.raises(HubError):
            validate_receive_commands(invalid, ref, "a" * 40)
    with w.owner() as db:
        stored = db.scalars(select(RepositoryTicket)).all()
        assert all(record.digest != ticket["ticket"] for record in stored)
