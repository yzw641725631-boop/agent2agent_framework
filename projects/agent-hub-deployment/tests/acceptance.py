"""Opt-in fixture for a disposable portable stack; never truncates a database.

Mount this file and agent-hub/tests into the operations container. See the
acceptance report for the operator command and strictly isolated test scope.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr
from sqlalchemy import select

sys.path.insert(0, "/backend-tests")
sys.path.insert(0, "/deployment")
from agent_hub.api import create_app
from agent_hub.db import coordinate, session_factory
from agent_hub.models import WorkSnapshot
from agent_hub.security import payload_hash, public_key
from agent_hub.work_archives import ArtifactStore
from conftest import PASSWORD, DeviceKey, Identity, World
from ops import archive_audit, connect, digest, fresh_target, secret, settings
from test_work_archives import archive_fixture, compress, setup_archive, upload

RECEIPT = Path("/staging/acceptance.json")


def guard():
    values = dict(line.split("=", 1) for line in Path("/config/settings.env").read_text().splitlines()
                  if line and not line.startswith("#"))
    if (os.environ.get("HUB_PORTABLE_ACCEPTANCE") != "isolated-only"
            or values.get("HUB_ENVIRONMENT") != "test"
            or values.get("HUB_TLS_MODE") != "local-test"
            or not values.get("HUB_STACK_NAME", "").startswith("hub-portable-qa-")
            or urlsplit(values.get("HUB_PUBLIC_URL", "")).hostname != "127.0.0.1"):
        raise RuntimeError("This fixture only accepts an explicit disposable loopback stack")
    return values


def wait_jobs(*, seconds=150):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with connect("agent_hub") as db:
            counts = db.execute("SELECT (SELECT count(*) FROM memory_jobs WHERE status='succeeded'),"
                                "(SELECT count(*) FROM memory_embeddings),"
                                "(SELECT count(*) FROM document_syncs WHERE status='succeeded'),"
                                "(SELECT count(*) FROM generation_calls)").fetchone()
        if counts[0] and counts[1] and counts[2]:
            if counts[3]:
                raise RuntimeError("Generation should remain disabled without an authorized provider")
            return {"memory_jobs_succeeded": counts[0], "semantic_vectors": counts[1],
                    "document_syncs_succeeded": counts[2], "generation_calls": counts[3]}
        time.sleep(2)
    raise RuntimeError("Background document/memory/embedding jobs did not finish")


def seed(values):
    fresh_target()
    cfg = settings().model_copy(update={"database_url": SecretStr(
        "postgresql+psycopg://hub_runtime:" + quote(Path("/config/secrets/runtime_password").read_text().strip(), safe="")
        + "@db:5432/agent_hub"), "public_url": values["HUB_PUBLIC_URL"]})
    owner = session_factory(settings())
    app = create_app(cfg)
    key = DeviceKey()
    with owner.begin() as db:
        coordinate(db)
        _, thumb = public_key(key.jwk)
        user, _ = app.state.hub.bootstrap(db, "portable-test-admin", PASSWORD, "portable@example.org", key.jwk, thumb, "windows")
        user_id = str(user.id)
    original = DeviceKey.proof

    def proof(self, method, path, token=None, **overrides):
        return original(self, method, path, token, htu=values["HUB_PUBLIC_URL"] + path.split("?")[0], **overrides)

    DeviceKey.proof = proof
    try:
        # Traffic crosses the real TLS gateway into the separate API container.
        with httpx.Client(base_url="https://gateway:8443", verify=False, trust_env=False,
                          headers={"Host": urlsplit(values["HUB_PUBLIC_URL"]).netloc}, timeout=60) as client:
            world = World(client, None, owner)
            _, enrollment = world.enroll("portable-test-admin", key)
            world.root = world.start(Identity(user_id, key, enrollment))
            binary = secrets.token_bytes(256 * 1024)

            def binary_archive():
                value = archive_fixture()
                value["contents"][0]["content_base64"] = base64.b64encode(binary).decode()
                value["evidence"]["files"][0].update(sha256=hashlib.sha256(binary).hexdigest(), bytes=len(binary))
                value["evidence_hash"] = payload_hash(value["evidence"])
                return value

            project, _, _, _, _, _, value, path = setup_archive(world, value_factory=binary_archive)
            response = upload(world, path, compress(value))
            if response.status_code != 200 or not response.json().get("server_code_backup"):
                raise RuntimeError("HTTPS binary work-tree upload did not succeed")
            base = f"/api/projects/{project}/documents"
            world.ok("PUT", base + "/repository", who=world.root, body={"expected_version": 0,
                "repository_url": "https://github.com/yzw641725631-boop/agent2agent_framework",
                "branch": "ac4053ac3160d7bda33f8ea9ffcf1ddbd8d1bc61", "include_paths": ["README.md"]})
            world.ok("POST", base + "/syncs", who=world.root, body={})
            jobs = wait_jobs()
            page = client.get("/")
            if page.status_code != 200 or "text/html" not in page.headers.get("Content-Type", ""):
                raise RuntimeError("HTTPS administration page did not load")
    finally:
        DeviceKey.proof = original
        owner.kw["bind"].dispose()
        app.state.sessions.kw["bind"].dispose()
    receipt = {"passed": True, "https_api_and_html": True, "binary_bytes": len(binary),
               "binary_sha256": hashlib.sha256(binary).hexdigest(), "jobs": jobs,
               "application_key_sha256": hashlib.sha256(secret("application_key").encode()).hexdigest()}
    RECEIPT.write_text(json.dumps(receipt))
    RECEIPT.chmod(0o600)
    return verify()


def verify():
    expected = json.loads(RECEIPT.read_text())
    cfg = settings()
    factory = session_factory(cfg)
    try:
        with factory() as db:
            rows = list(db.scalars(select(WorkSnapshot)))
        if len(rows) != 1:
            raise RuntimeError("Expected exactly one synthetic archive")
        value = ArtifactStore(cfg).read(rows[0])
        # ArtifactStore authenticates/decrypts and returns the original compressed archive.
        import gzip
        decoded = json.loads(gzip.decompress(value))
        binary = base64.b64decode(decoded["contents"][0]["content_base64"])
        if len(binary) != expected["binary_bytes"] or hashlib.sha256(binary).hexdigest() != expected["binary_sha256"]:
            raise RuntimeError("Restored binary bytes differ from the HTTPS upload")
        if hashlib.sha256(secret("application_key").encode()).hexdigest() != expected["application_key_sha256"]:
            raise RuntimeError("Source application encryption key was not retained")
        archive_hash = digest(ArtifactStore(cfg).file(rows[0].id))
        if expected.get("encrypted_archive_sha256", archive_hash) != archive_hash:
            raise RuntimeError("Encrypted archive bytes changed")
        with connect("agent_hub") as db:
            roles = db.execute("SELECT count(*) FROM pg_roles WHERE rolname IN "
                "('hub_runtime','hub_document_worker','hub_memory_worker','hub_embedding_worker','hub_generation_worker') "
                "AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole").fetchone()[0]
            rls = db.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='worklogs'::regclass").fetchone()[0]
        if roles != 5 or not rls:
            raise RuntimeError("Runtime database role/RLS restrictions differ")
        result = {**expected, "encrypted_archive_sha256": archive_hash, "runtime_roles_restricted": True,
                  "forced_rls": True, "archive_audit": archive_audit(), "binary_roundtrip_verified": True}
        RECEIPT.write_text(json.dumps(result))
        return result
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("seed", "verify"))
    args = parser.parse_args()
    values = guard()
    print(json.dumps(seed(values) if args.phase == "seed" else verify()), flush=True)
