"""Real Git -> live ASGI gateway -> real stateless Git, using the isolated hub_test DB."""

import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import httpx
import uvicorn
from sqlalchemy import select
from test_repositories import basic, grant, register, task
from test_tasks import claim, lease, log_body
from test_work_policy import observed_body, prepare, publish

from agent_hub.db import context
from agent_hub.models import Task
from agent_hub.security import now


@contextmanager
def live_gateway(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_real_git_gateway_clone_fetch_push_and_refusal_before_upstream_write(world, tmp_path):
    w, p = world, world.project()
    repo = register(w, p)
    member = w.member(p)
    w.start(member, grant(w, member, p, [(repo, ["code:read", "code:write"])]))
    source, bare, clone = (tmp_path / name for name in ("source", "upstream.git", "clone"))
    source.mkdir()
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    clean_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")

    def git(*args, cwd=source, auth=None, success=True):
        env = dict(clean_env)
        if auth:
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0="Authorization: " + basic(auth),
            )
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=15)
        assert (result.returncode == 0) == success, "Git result differs from expected success/refusal"
        return result.stdout.decode().strip()

    git("init", "-q", "--initial-branch=main")
    (source / "code.txt").write_text("baseline\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    git("clone", "--bare", "-q", str(source), str(bare))
    requests = []

    def upstream(request):
        assert request.url.host == "github.com"
        assert request.url.path.startswith("/example/web.git/")
        write = (
            request.url.path.endswith("git-receive-pack")
            or request.url.params.get("service") == "git-receive-pack"
        )
        service = "git-receive-pack" if write else "git-upload-pack"
        command = "receive-pack" if write else "upload-pack"
        advertisement = request.method == "GET"
        requests.append((request.method, service))
        env = dict(clean_env)
        if request.headers.get("git-protocol") == "version=2":
            env["GIT_PROTOCOL"] = "version=2"
        process = subprocess.run(
            ["git", command, "--stateless-rpc", *(["--advertise-refs"] if advertisement else []), str(bare)],
            input=b"" if advertisement else request.content,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert process.returncode == 0
        body = process.stdout
        if advertisement:
            prefix = ("# service=" + service + "\n").encode()
            body = f"{len(prefix) + 4:04x}".encode() + prefix + b"0000" + body
        return httpx.Response(
            200,
            headers={
                "Content-Type": f"application/x-{service}-" + ("advertisement" if advertisement else "result")
            },
            content=body,
        )

    w.client.app.state.git_transport = httpx.MockTransport(upstream)
    ticket_path = f"/api/projects/{p}/repositories/{repo['id']}/git-ticket"
    read = w.ok("POST", ticket_path, who=member, body={})
    t = task(w, p, repo)
    run = claim(w, p, t, member)
    with live_gateway(w.client.app) as base:
        origin = base + read["git_path"]
        git("clone", "--single-branch", "--branch", "main", origin, str(clone), auth=read["ticket"])
        assert git("rev-parse", "HEAD", cwd=clone) == baseline
        (clone / "code.txt").write_text("task changes\n")
        git("fetch", "--no-tags", origin, "main", cwd=clone, auth=read["ticket"])
        assert (clone / "code.txt").read_text() == "task changes\n"
        git("add", ".", cwd=clone)
        git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "task", cwd=clone)
        commit = git("rev-parse", "HEAD", cwd=clone)
        w.ok(
            "POST",
            f"/api/projects/{p}/tasks/{t['id']}/worklogs",
            who=member,
            body=log_body(run, changed_paths=["src/task.ts"], git_commit=commit),
        )
        body = {"action": "write", "task_id": t["id"], **lease(run), "expected_commit": commit}
        write = w.ok("POST", ticket_path, who=member, body=body)
        ref = write["write_ref"]
        git("push", origin, "HEAD:" + ref, cwd=clone, auth=write["ticket"])
        assert git("--git-dir=" + str(bare), "rev-parse", ref) == commit
        assert git("--git-dir=" + str(bare), "rev-parse", "main") == baseline
        count = requests.count(("POST", "git-receive-pack"))
        for refspec in ("HEAD:refs/heads/main", "HEAD:refs/heads/hub/somebody-else", ":" + ref):
            git("push", origin, refspec, cwd=clone, auth=write["ticket"], success=False)
        git("push", origin, "HEAD:refs/heads/read-only", cwd=clone, auth=read["ticket"], success=False)
        assert requests.count(("POST", "git-receive-pack")) == count
        # An old write ticket cannot outlive a newer log or a task lease.
        w.ok(
            "POST",
            f"/api/projects/{p}/tasks/{t['id']}/worklogs",
            who=member,
            body=log_body(run, git_commit="b" * 40),
        )
        git("ls-remote", origin, cwd=clone, auth=write["ticket"])
        assert (
            w.client.get(
                read["git_path"] + "/info/refs?service=git-receive-pack",
                headers={"Authorization": basic(write["ticket"])},
            ).status_code
            == 409
        )
        w.ok(
            "POST",
            f"/api/projects/{p}/tasks/{t['id']}/worklogs",
            who=member,
            body=log_body(run, git_commit=commit),
        )
        with w.client.app.state.sessions.begin() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            db.scalar(select(Task).where(Task.id == t["id"])).lease_until = now() - timedelta(seconds=1)
        assert (
            w.client.get(
                read["git_path"] + "/info/refs?service=git-receive-pack",
                headers={"Authorization": basic(write["ticket"])},
            ).status_code
            == 409
        )
        assert requests.count(("POST", "git-receive-pack")) == count
        # Archiving/revoking also prevents read access; no request reaches upstream.
        w.ok(
            "POST",
            f"/api/projects/{p}/repositories/{repo['id']}/status",
            who=w.root,
            body={"status": "archived", "expected_version": 1},
        )
        before = len(requests)
        git("ls-remote", origin, cwd=clone, auth=read["ticket"], success=False)
        w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
        git("ls-remote", origin, cwd=clone, auth=read["ticket"], success=False)
        assert len(requests) == before


def test_write_ticket_rechecks_current_rule_acknowledgment(world):
    w, p = world, world.project()
    repo = register(w, p)
    t = task(w, p, repo, execution_mode="observed")
    run = claim(w, p, t)
    prepared = prepare(w, p, run)
    w.ok("POST", f"/api/projects/{p}/tasks/{t['id']}/worklogs", who=w.root, body=observed_body(run, prepared))
    route = f"/api/projects/{p}/repositories/{repo['id']}/git-ticket"
    body = {"action": "write", "task_id": t["id"], **lease(run), "expected_commit": "b" * 40}
    ticket = w.ok("POST", route, who=w.root, body=body)
    publish(w, p)
    upstream_requests = []
    w.client.app.state.git_transport = httpx.MockTransport(lambda request: upstream_requests.append(request))
    response = w.client.get(
        ticket["git_path"] + "/info/refs?service=git-receive-pack",
        headers={"Authorization": basic(ticket["ticket"])},
    )
    assert response.json()["code"] == "WORK_CONTEXT_STALE"
    assert w.call("POST", route, who=w.root, body=body).json()["code"] == "WORK_CONTEXT_STALE"
    assert not upstream_requests
