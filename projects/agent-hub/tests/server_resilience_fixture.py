"""Disposable server drill. Credentials and synchronization files stay in /state.

The API process loads the normal application without test routes or patched methods.
Use scripts/run-server-resilience.py on the authorized Linux test server.
"""

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import quote

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from conftest import PASSWORD, DeviceKey, Identity, World
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from test_repositories import grant, register
from test_tasks import claim, log_body, ready
from test_work_archives import archive_fixture, compress
from test_work_policy import observed_body, prepare

from agent_hub import SCHEMA_REVISION, __version__
from agent_hub.api import create_app
from agent_hub.config import Settings, get_settings
from agent_hub.db import coordinate
from agent_hub.security import payload_hash, public_key

ORIGIN = "http://127.0.0.1:18768"
STATE = Path("/state")


def guard():
    if sys.platform != "linux" or os.environ.get("HUB_SERVER_RESILIENCE") != "disposable-only":
        raise RuntimeError("Explicit opt-in on the isolated Linux test server is required")
    if os.environ.get("POSTGRES_DB") != "hub_test":
        raise RuntimeError("Only the disposable hub_test database is allowed")


def url(role):
    value = (
        "postgresql+psycopg://hub_"
        + role.lower()
        + ":"
        + quote(os.environ["HUB_" + role + "_PASSWORD"], safe="")
        + "@postgres:5432/hub_test"
    )
    if make_url(value).database != "hub_test":
        raise RuntimeError("Invalid test database")
    return value


def settings():
    return Settings(
        database_url=url("RUNTIME"),
        public_url=ORIGIN,
        environment="test",
        mail_mode="memory",
        artifact_directory="/state/artifacts",
        access_seconds=1800,
    )


def save(name, value):
    target = STATE / name
    temporary = target.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(target)


def wait_file(name, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (STATE / name).is_file():
            return json.loads((STATE / name).read_text())
        time.sleep(0.1)
    raise RuntimeError("Operator synchronization timed out: " + name)


def seed():
    engine = create_engine(url("OWNER"), hide_parameters=True)
    with engine.begin() as db:
        if db.scalar(text("SELECT current_database()")) != "hub_test":
            raise RuntimeError("Not the disposable database")
        if db.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")):
            raise RuntimeError("Seeding requires a new empty test database")
    os.environ["HUB_DATABASE_URL"] = url("OWNER")
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    owner = sessionmaker(engine, expire_on_commit=False)
    cfg = settings()
    Path(cfg.artifact_directory).mkdir(mode=0o700)
    app = create_app(cfg)
    original = DeviceKey.proof

    def proof(self, method, path, token=None, **overrides):
        return original(self, method, path, token, htu=ORIGIN + path.split("?")[0], **overrides)

    DeviceKey.proof = proof
    try:
        with TestClient(app) as client:
            world = World(client, app.state.mailer, owner)
            key = DeviceKey()
            with owner.begin() as db:
                coordinate(db)
                _, thumb = public_key(key.jwk)
                user, _ = app.state.hub.bootstrap(
                    db, "resilience-root", PASSWORD, "resilience@example.org", key.jwk, thumb, "windows"
                )
                user_id = str(user.id)
            _, enrollment = world.enroll("resilience-root", key)
            world.root = world.start(Identity(user_id, key, enrollment))

            def profile(identity):
                return {
                    "user_id": identity.user_id,
                    "device_id": identity.enrollment["device_id"],
                    "tokens": identity.tokens,
                    "private_key": identity.key.key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    ).decode(),
                }

            result = {"tenants": [], "version": __version__, "schema": SCHEMA_REVISION}
            for label in ("a", "b"):
                project = world.project("resilience-" + label)
                repo = register(world, project)
                member = world.member(project, login="resilience-member-" + label)
                world.start(member, grant(world, member, project, [(repo, ["code:read", "code:write"])]))
                load_task = ready(
                    world,
                    project,
                    repository_id=repo["id"],
                    title="SYNTHETIC-TENANT-" + label,
                    scope={"repository": "web", "platform": "web", "paths": ["src/load"]},
                )
                load_run = claim(world, project, load_task, member)
                archive_task = ready(
                    world,
                    project,
                    repository_id=repo["id"],
                    title="synthetic archive " + label,
                    execution_mode="observed",
                    scope={"repository": "web", "platform": "web", "paths": ["src/login.ts"]},
                )
                archive_run = claim(world, project, archive_task, member)
                prepared = prepare(world, project, archive_run, member)
                value = archive_fixture()
                changed = secrets.token_bytes(2 * 1024 * 1024)
                value["contents"][0]["content_base64"] = base64.b64encode(changed).decode()
                value["evidence"]["files"][0].update(
                    sha256=hashlib.sha256(changed).hexdigest(), bytes=len(changed)
                )
                value["evidence_hash"] = payload_hash(value["evidence"])
                body = observed_body(archive_run, prepared)
                body.update(change_evidence=value["evidence"], git_commit=value["evidence"]["head_commit"])
                path = f"/api/projects/{project}/tasks/{archive_task['id']}/worklogs"
                log = world.ok("POST", path, who=member, body=body)
                value["worklog_id"] = log["id"]
                archive = compress(value)
                archive_file = STATE / ("archive-" + label + ".gz")
                archive_file.write_bytes(archive)
                archive_file.chmod(0o600)
                entry = {
                    "label": label,
                    "project_id": project,
                    "repository_id": repo["id"],
                    "identity": profile(member),
                    "task": load_task,
                    "run": load_run,
                    "log_path": f"/api/projects/{project}/tasks/{load_task['id']}/worklogs",
                    "archive_path": path + "/" + log["id"] + "/snapshot",
                    "archive_file": archive_file.name,
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "archive_compressed_bytes": len(archive),
                    "archive_changed_bytes": len(changed),
                }
                if label == "a":
                    rival = world.member(project, login="resilience-rival")
                    world.start(rival, grant(world, rival, project, [(repo, ["code:read", "code:write"])]))
                    entry["rival"] = profile(rival)
                    for kind in ("race", "idempotent"):
                        entry[kind] = ready(
                            world,
                            project,
                            repository_id=repo["id"],
                            title="synthetic " + kind,
                            scope={"repository": "web", "platform": "web", "paths": ["src/" + kind]},
                        )
                result["tenants"].append(entry)
            save("seed.json", result)
    finally:
        DeviceKey.proof = original
        app.state.sessions.kw["bind"].dispose()
        engine.dispose()
    print("Disposable identities and tasks seeded; credentials retained only in private state.", flush=True)


class Driver:
    def __init__(self):
        self.seed = json.loads((STATE / "seed.json").read_text())
        self.tenants = self.seed["tenants"]
        self.client = httpx.Client(
            base_url="http://api:18768",
            timeout=20,
            trust_env=False,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=32),
        )
        self.owner = create_engine(url("OWNER"), hide_parameters=True, pool_pre_ping=True)
        self.keys = {}
        for tenant in self.tenants:
            for who in [tenant["identity"], *([tenant["rival"]] if "rival" in tenant else [])]:
                key = DeviceKey()
                key.key = serialization.load_pem_private_key(who["private_key"].encode(), password=None)
                key.jwk = json.loads(__import__("jwt").algorithms.ECAlgorithm.to_jwk(key.key.public_key()))
                self.keys[who["user_id"]] = key
        self.report = {
            "passed": False,
            "version": self.seed["version"],
            "schema": self.seed["schema"],
            "concurrency": [],
            "checks": [],
            "faults": [],
        }

    def call(self, who, method, path, *, body=None, idem=None, data=None):
        token = who["tokens"]["access_token"]
        key = self.keys[who["user_id"]]
        headers = {
            "Authorization": "DPoP " + token,
            "DPoP": key.proof(method, path, token, htu=ORIGIN + path.split("?")[0]),
        }
        if method != "GET":
            headers["Idempotency-Key"] = idem or str(uuid.uuid4())
        if data is not None:
            headers.update(
                {"Content-Type": "application/gzip", "X-Content-SHA256": hashlib.sha256(data).hexdigest()}
            )
            return self.client.request(method, path, headers=headers, content=data)
        return self.client.request(method, path, headers=headers, json=body)

    def ok(self, who, method, path, **kwargs):
        for attempt in range(5):
            response = self.call(who, method, path, **kwargs)
            if response.status_code != 503:
                break
            if response.json().get("code") != "SERVICE_BUSY":
                raise AssertionError("Unexpected busy response")
            time.sleep(0.2 * (attempt + 1))
        if response.status_code != 200:
            raise AssertionError(f"Expected successful {method}; received HTTP {response.status_code}")
        return response.json()

    def check(self, name, **evidence):
        self.report["checks"].append({"name": name, "passed": True, **evidence})
        print("PASS " + name, flush=True)

    @staticmethod
    def body(tenant, summary):
        return log_body(
            tenant["run"],
            summary=summary,
            notes="synthetic-tenant-" + tenant["label"],
            changed_paths=["src/load/check.txt"],
        )

    def scalar(self, query, **params):
        with self.owner.connect() as db:
            return db.scalar(text(query), params)

    def parallel(self, count, function):
        barrier = Barrier(count)

        def run(index):
            barrier.wait(timeout=15)
            return function(index)

        with ThreadPoolExecutor(count) as pool:
            return list(pool.map(run, range(count)))

    def functional(self):
        tenant = self.tenants[0]
        project = tenant["project_id"]
        who = tenant["identity"]
        path = f"/api/projects/{project}/tasks/{tenant['race']['id']}/claim"

        def compete(index):
            actor = who if index % 2 == 0 else tenant["rival"]
            body = {"instance_id": str(uuid.uuid4()), "harness": "codex"}
            operation = str(uuid.uuid4())
            for _ in range(5):
                response = self.call(actor, "POST", path, body=body, idem=operation)
                if response.status_code != 503:
                    return response
                time.sleep(0.2)
            return response

        responses = self.parallel(16, compete)
        assert Counter(r.status_code for r in responses) == {200: 1, 409: 15}
        assert all(r.json()["code"] == "TASK_ALREADY_CLAIMED" for r in responses if r.status_code == 409)
        assert (
            self.scalar(
                "SELECT count(*) FROM task_runs WHERE task_id=:task AND state='active'",
                task=tenant["race"]["id"],
            )
            == 1
        )
        self.check("sixteen_competing_claims_have_one_active_owner", contenders=16, winners=1)

        path = f"/api/projects/{project}/tasks/{tenant['idempotent']['id']}/claim"
        body = {"instance_id": str(uuid.uuid4()), "harness": "codex"}
        operation = str(uuid.uuid4())
        results = self.parallel(16, lambda _: self.ok(who, "POST", path, body=body, idem=operation))
        assert all(result == results[0] for result in results)
        assert (
            self.scalar(
                "SELECT count(*) FROM task_runs WHERE task_id=:task AND state='active'",
                task=tenant["idempotent"]["id"],
            )
            == 1
        )
        self.check("sixteen_identical_claims_reuse_one_run", requests=16)

        operation = str(uuid.uuid4())
        body = self.body(tenant, "resilience-idempotent-log")
        logs = self.parallel(
            16, lambda _: self.ok(who, "POST", tenant["log_path"], body=body, idem=operation)
        )
        assert all(log == logs[0] for log in logs)
        assert self.scalar("SELECT count(*) FROM worklogs WHERE summary='resilience-idempotent-log'") == 1
        self.check("sixteen_identical_worklogs_reuse_one_record", requests=16)

        def upload(index):
            selected = self.tenants[index % 2]
            data = (STATE / selected["archive_file"]).read_bytes()
            return self.ok(
                selected["identity"],
                "POST",
                selected["archive_path"],
                data=data,
                idem=str(uuid.uuid5(uuid.NAMESPACE_URL, selected["archive_path"])),
            )

        receipts = self.parallel(4, upload)
        assert receipts[0] == receipts[2] and receipts[1] == receipts[3]
        assert receipts[0]["id"] != receipts[1]["id"]
        assert self.scalar("SELECT count(*) FROM work_snapshots") == 2
        self.receipts = {tenant["label"]: receipts[index] for index, tenant in enumerate(self.tenants)}
        self.verify_archives()
        self.check(
            "concurrent_binary_backups_are_durable_and_idempotent",
            concurrent_uploads=4,
            distinct_snapshots=2,
            changed_bytes_per_snapshot=2 * 1024 * 1024,
            compressed_bytes_per_snapshot=[tenant["archive_compressed_bytes"] for tenant in self.tenants],
        )

    def verify_archives(self):
        for tenant in self.tenants:
            assert (
                self.ok(tenant["identity"], "GET", tenant["archive_path"]) == self.receipts[tenant["label"]]
            )
            response = self.call(tenant["identity"], "GET", tenant["archive_path"] + "/content")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == tenant["archive_sha256"]
            other = self.tenants[1] if tenant["label"] == "a" else self.tenants[0]
            denied = self.call(other["identity"], "GET", tenant["archive_path"] + "/content")
            assert denied.status_code == 404

    def workload(self):
        for concurrency in (1, 4, 8, 16, 32):
            writes = []

            def request(index, *, tier=concurrency, accepted_writes=writes):
                tenant = self.tenants[index % 2]
                other = self.tenants[(index + 1) % 2]
                who = tenant["identity"]
                kind = index % 5
                base = tenant["log_path"].removesuffix("/worklogs")
                expected, method, path, body = 200, "GET", base, None
                if kind == 1:
                    path = tenant["log_path"]
                elif kind == 2:
                    method, path = "POST", tenant["log_path"]
                    body = self.body(tenant, f"resilience-load-{tier}-{index}")
                elif kind == 3:
                    expected, path = 404, other["log_path"].removesuffix("/worklogs")
                elif kind == 4:
                    path = f"/api/projects/{tenant['project_id']}/repositories"
                started = time.perf_counter()
                response = self.call(who, method, path, body=body)
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code == 503:
                    assert response.json().get("code") == "SERVICE_BUSY"
                    assert response.headers.get("Retry-After") == "3"
                else:
                    assert response.status_code == expected, f"Unexpected load status: {response.status_code}"
                    assert ("SYNTHETIC-TENANT-" + other["label"]) not in response.text
                    assert ("synthetic-tenant-" + other["label"]) not in response.text
                    if expected == 200 and kind == 0:
                        assert response.json()["task"]["id"] == tenant["task"]["id"]
                    if method == "POST":
                        accepted_writes.append(response.json()["id"])
                return elapsed, response.status_code, method, kind

            started = time.perf_counter()
            with ThreadPoolExecutor(concurrency) as pool:
                results = list(pool.map(request, range(160)))
            seconds = time.perf_counter() - started
            latencies = sorted(row[0] for row in results)
            statuses = Counter(str(row[1]) for row in results)
            count = self.scalar(
                "SELECT count(*) FROM worklogs WHERE summary LIKE :prefix",
                prefix=f"resilience-load-{concurrency}-%",
            )
            assert count == len(writes) and len(set(writes)) == len(writes)
            assert statuses.get("200", 0) + statuses.get("404", 0) > 0
            if concurrency == 1:
                assert not statuses.get("503", 0)
            summary = {
                "concurrency": concurrency,
                "requests": len(results),
                "elapsed_seconds": round(seconds, 3),
                "requests_per_second": round(len(results) / seconds, 2),
                "latency_p50_ms": round(latencies[math.ceil(len(latencies) * 0.5) - 1], 2),
                "latency_p95_ms": round(latencies[math.ceil(len(latencies) * 0.95) - 1], 2),
                "latency_max_ms": round(max(latencies), 2),
                "http_status_counts": dict(statuses),
                "acknowledged_writes": len(writes),
                "persisted_writes": count,
                "expected_denials": sum(row[1] == 404 for row in results),
                "busy_responses": statuses.get("503", 0),
                "unexpected_errors": 0,
            }
            self.report["concurrency"].append(summary)
            save("progress.json", self.report)
            print("LOAD " + json.dumps(summary, separators=(",", ":")), flush=True)
        self.check("eight_hundred_mixed_requests_preserve_isolation_and_write_counts", requests=800)

    def overload(self):
        tenant = self.tenants[0]
        base = tenant["log_path"].removesuffix("/worklogs")
        started = time.perf_counter()
        with self.owner.connect() as lock:
            lock.execute(text("SELECT pg_advisory_xact_lock(7206110501)"))
            responses = self.parallel(32, lambda _: self.call(tenant["identity"], "GET", base))
            assert all(response.status_code == 503 for response in responses)
            assert all(response.json().get("code") == "SERVICE_BUSY" for response in responses)
            assert all(response.headers.get("Retry-After") == "3" for response in responses)
        recovery = time.perf_counter()
        for _ in range(5):
            self.ok(tenant["identity"], "GET", base)
        self.check(
            "blocked_database_locks_return_bounded_busy_and_recover",
            requests=32,
            busy_responses=32,
            failure_batch_seconds=round(recovery - started, 3),
            five_recovery_reads_seconds=round(time.perf_counter() - recovery, 3),
        )

    def fault(self, kind):
        tenant = self.tenants[0]
        who = tenant["identity"]
        operation = str(uuid.uuid4())
        summary = "resilience-before-" + kind
        body = self.body(tenant, summary)
        before = self.ok(who, "POST", tenant["log_path"], body=body, idem=operation)
        assert self.scalar("SELECT count(*) FROM worklogs WHERE summary=:summary", summary=summary) == 1
        save("await-" + kind + ".json", {"committed": True})
        wait_file(kind + "-down.json")
        pending_operation = str(uuid.uuid4())
        pending_summary = "resilience-during-" + kind
        pending_body = self.body(tenant, pending_summary)
        failure_started = time.perf_counter()
        if kind == "api":
            try:
                self.call(who, "POST", tenant["log_path"], body=pending_body, idem=pending_operation)
            except httpx.TransportError as error:
                failure = type(error).__name__
            else:
                raise AssertionError("Stopped API unexpectedly accepted a request")
        else:
            response = self.call(who, "POST", tenant["log_path"], body=pending_body, idem=pending_operation)
            assert response.status_code == 503 and response.json().get("code") == "SERVICE_BUSY"
            assert response.headers.get("Retry-After") == "3"
            failure = "503 SERVICE_BUSY"
        save(kind + "-down-observed.json", {"failure": failure})
        operator = wait_file(kind + "-restarted.json")
        started = time.perf_counter()
        deadline = started + 35
        while True:
            try:
                after = self.ok(who, "POST", tenant["log_path"], body=body, idem=operation)
                break
            except (httpx.TransportError, AssertionError):
                if time.perf_counter() > deadline:
                    raise
                time.sleep(0.2)
        assert before == after
        first_success_seconds = time.perf_counter() - started
        pending = self.ok(who, "POST", tenant["log_path"], body=pending_body, idem=pending_operation)
        assert self.ok(who, "POST", tenant["log_path"], body=pending_body, idem=pending_operation) == pending
        assert self.scalar("SELECT count(*) FROM worklogs WHERE summary=:summary", summary=summary) == 1
        assert (
            self.scalar("SELECT count(*) FROM worklogs WHERE summary=:summary", summary=pending_summary) == 1
        )
        self.verify_archives()
        result = {
            "kind": kind,
            "passed": True,
            "failure_observed": failure,
            "outage_observed_through_verification_seconds": round(time.perf_counter() - failure_started, 3),
            "authenticated_retry_after_restart_seconds": round(first_success_seconds, 3),
            "post_restart_complete_verification_seconds": round(time.perf_counter() - started, 3),
            "committed_log_reused": True,
            "retried_during_outage_log_count": 1,
            "both_backup_bytes_and_receipts_preserved": True,
            "operator": operator,
        }
        self.report["faults"].append(result)
        self.check(kind + "_sigkill_restart_preserves_commits_and_backups")

    def run(self):
        started = time.perf_counter()
        try:
            self.functional()
            self.workload()
            self.overload()
            self.fault("api")
            self.fault("database")
            assert self.scalar("SELECT count(*) FROM work_snapshots") == 2
            self.report.update(
                passed=True,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                worklogs_final=self.scalar("SELECT count(*) FROM worklogs"),
                snapshots_final=2,
                synthetic_access_seconds=1800,
            )
            save("result.json", self.report)
            print("PASS server resilience drill", flush=True)
        except Exception as error:
            self.report["failure_type"] = type(error).__name__
            save("result.json", self.report)
            raise
        finally:
            self.client.close()
            self.owner.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "serve", "drive"))
    args = parser.parse_args()
    guard()
    if args.mode == "seed":
        seed()
    elif args.mode == "serve":
        uvicorn.run(create_app(settings()), host="0.0.0.0", port=18768, access_log=False)
    else:
        Driver().run()


if __name__ == "__main__":
    main()
