"""Opt-in, disposable HTTP fixture for the Windows collaboration integration driver.

Only use on the isolated test network with a database named hub_test. The seed
contains synthetic device credentials and requires a separate fixture key.
"""

import os
import secrets
from pathlib import Path
from urllib.parse import quote

import uvicorn
from alembic import command
from alembic.config import Config
from conftest import PASSWORD, DeviceKey, Identity, World
from cryptography.hazmat.primitives import serialization
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_repositories import grant, register, task

from agent_hub.api import create_app
from agent_hub.config import Settings, get_settings
from agent_hub.db import coordinate
from agent_hub.mail import Mailer
from agent_hub.models import Base
from agent_hub.security import public_key

ORIGIN = "http://127.0.0.1:18768"


def main():
    if os.environ.get("HUB_TEST_HTTP") != "collaboration-only":
        raise RuntimeError("Disposable collaboration fixture requires explicit opt-in")
    fixture_key = os.environ["HUB_TEST_FIXTURE_KEY"]
    if len(fixture_key) < 40:
        raise RuntimeError("A separate random fixture key is required")
    urls = {
        name: "postgresql+psycopg://hub_" + role + ":"
        + quote(os.environ["HUB_" + name + "_PASSWORD"], safe="")
        + "@postgres:5432/hub_test"
        for name, role in [("OWNER", "owner"), ("RUNTIME", "runtime")]
    }
    engine = create_engine(urls["OWNER"], hide_parameters=True)
    with engine.begin() as db:
        if db.scalar(text("SELECT current_database()")) != "hub_test":
            raise RuntimeError("Only the disposable hub_test database is allowed")
        if db.scalar(text("SELECT current_user")) != "hub_owner":
            raise RuntimeError("The disposable fixture requires its test owner role")
    os.environ["HUB_DATABASE_URL"] = urls["OWNER"]
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    with engine.begin() as db:
        names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE TABLE {names} CASCADE"))

    owner = sessionmaker(engine, expire_on_commit=False)
    cfg = Settings(database_url=urls["RUNTIME"], public_url=ORIGIN,
                   environment="test", mail_mode="memory",
                   artifact_directory="/tmp/collaboration-artifacts")
    Path(cfg.artifact_directory).mkdir(mode=0o700, exist_ok=True)
    mail = Mailer(cfg)
    app = create_app(cfg, mailer=mail)
    original = DeviceKey.proof

    def proof(self, method, path, token=None, **overrides):
        return original(self, method, path, token, htu=ORIGIN + path.split("?")[0], **overrides)

    DeviceKey.proof = proof
    try:
        with TestClient(app) as client:
            w = World(client, mail, owner)
            key = DeviceKey()
            with owner.begin() as db:
                coordinate(db)
                _, thumb = public_key(key.jwk)
                user, _ = app.state.hub.bootstrap(
                    db, "fixture-root", PASSWORD, "fixture@example.org", key.jwk, thumb, "windows")
                uid = str(user.id)
            _, enrollment = w.enroll("fixture-root", key)
            w.root = w.start(Identity(uid, key, enrollment))
            project = w.project("collaboration-acceptance")
            web, api, private = (register(w, project, slug) for slug in ["web", "api", "pc"])
            capabilities = ["project:read", "tasks:claim", "tasks:write", "tasks:review",
                            "tasks:test", "logs:read", "documents:read"]

            def profile(identity, platform):
                return {"origin": ORIGIN, "login": "synthetic-fixture",
                        "user": {"id": identity.user_id},
                        "deviceId": identity.enrollment["device_id"],
                        "identity": {"jwk": identity.key.jwk, "privateKey": identity.key.key.private_bytes(
                            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()},
                        "sessions": {"platform": platform, project: identity.tokens},
                        "bindings": [], "events": [], "workRuns": {}}

            def member(login, permissions):
                identity = w.member(project, login=login)
                platform = identity.tokens.copy()
                w.start(identity, grant(w, identity, project, permissions, caps=capabilities))
                return profile(identity, platform)

            permissions = [(repo, ["code:read", "code:write", "documents:read"]) for repo in [web, api]]
            author = member("fixture-author", permissions)
            colleague = member("fixture-colleague", permissions)
            restricted = member("fixture-restricted", [(private, ["code:read"])])
            first = task(w, project, web, title="合成 Web 任务", execution_mode="observed")
            following = task(w, project, api, title="合成 API 接续任务", execution_mode="observed",
                             depends_on=[first["id"]])
            private_task = task(w, project, private, title="private-fixture-task")
            seed = {"fixture_kind": "isolated-collaboration-v1", "project_id": project,
                    "repositories": {"web": web, "api": api, "private": private},
                    "tasks": {"web": first, "api": following, "private": private_task},
                    "root": profile(w.root, w.root.tokens), "author": author,
                    "colleague": colleague, "restricted": restricted}
    finally:
        DeviceKey.proof = original

    @app.get("/__test__/seed")
    def seeded(x_hub_fixture_key: str = Header(default="")):
        if not secrets.compare_digest(x_hub_fixture_key, fixture_key):
            raise HTTPException(status_code=404)
        return seed

    uvicorn.run(app, host="0.0.0.0", port=18768, access_log=False)


if __name__ == "__main__":
    main()
