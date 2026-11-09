"""Opt-in disposable HTTP fixture for real desktop/server handoff verification."""
import os
from pathlib import Path
from urllib.parse import quote

import uvicorn
from alembic import command
from alembic.config import Config
from conftest import PASSWORD, DeviceKey, Identity, World
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_repositories import grant, register, task
from test_tasks import claim

from agent_hub.api import create_app
from agent_hub.config import Settings
from agent_hub.db import coordinate
from agent_hub.mail import Mailer
from agent_hub.security import public_key


def main():
    if os.environ.get("HUB_TEST_HTTP") != "handoff-only":
        raise RuntimeError("Disposable handoff fixture requires explicit test opt-in")
    origin = "http://127.0.0.1:18767"
    owner_url = "postgresql+psycopg://hub_owner:" + quote(os.environ["HUB_OWNER_PASSWORD"], safe="") + "@postgres:5432/hub_test"
    runtime_url = "postgresql+psycopg://hub_runtime:" + quote(os.environ["HUB_RUNTIME_PASSWORD"], safe="") + "@postgres:5432/hub_test"
    engine = create_engine(owner_url, hide_parameters=True)
    with engine.begin() as db:
        assert db.scalar(text("SELECT current_database()")) == "hub_test"
        db.execute(text("DROP SCHEMA IF EXISTS hub CASCADE"))
        db.execute(text("DROP SCHEMA public CASCADE"))
        db.execute(text("CREATE SCHEMA public AUTHORIZATION hub_owner"))
    os.environ["HUB_DATABASE_URL"] = owner_url
    from agent_hub.config import get_settings
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    owner = sessionmaker(engine, expire_on_commit=False)
    cfg = Settings(database_url=runtime_url, public_url=origin, environment="test", mail_mode="memory",
                   artifact_directory="/tmp/handoff-artifacts")
    Path(cfg.artifact_directory).mkdir(mode=0o700, exist_ok=True)
    mail = Mailer(cfg)
    app = create_app(cfg, mailer=mail)
    original = DeviceKey.proof
    DeviceKey.proof = lambda self, method, path, token=None, **kw: original(self, method, path, token, htu=origin + path.split("?")[0], **kw)
    with TestClient(app) as client:
        w = World(client, mail, owner)
        root_key = DeviceKey()
        with owner.begin() as db:
            coordinate(db)
            _, thumb = public_key(root_key.jwk)
            user, _ = app.state.hub.bootstrap(db, "fixture-root", PASSWORD, "fixture@example.org", root_key.jwk, thumb, "windows")
            uid = str(user.id)
        _, enrollment = w.enroll("fixture-root", root_key)
        w.root = w.start(Identity(uid, root_key, enrollment))
        project = w.project("durable-handoff")
        repo = register(w, project)
        author = w.member(project, login="handoff-author")
        author_platform = author.tokens.copy()
        w.start(author, grant(w, author, project, [(repo, ["code:read", "code:write"])]))
        reader = w.member(project, login="handoff-reader")
        reader_platform = reader.tokens.copy()
        w.start(reader, grant(w, reader, project, [(repo, ["code:read"])], caps=["project:read", "logs:read"]))
        restricted = w.member(project, login="handoff-restricted")
        restricted_platform = restricted.tokens.copy()
        pc = register(w, project, "pc")
        w.start(restricted, grant(w, restricted, project, [(pc, ["code:read"])], caps=["project:read", "logs:read"]))
        work = task(w, project, repo, execution_mode="observed")
        run = claim(w, project, work, author)

        def profile(identity, platform):
            return {"origin": origin, "login": "fixture", "user": {"id": identity.user_id},
                "deviceId": identity.enrollment["device_id"],
                "identity": {"jwk": identity.key.jwk, "privateKey": identity.key.key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()},
                "sessions": {"platform": platform, project: identity.tokens}, "bindings": [], "events": [], "workRuns": {}}

        seed = {"project_id": project, "repository_id": repo["id"], "task": work, "run": run,
                "author": profile(author, author_platform), "reader": profile(reader, reader_platform),
                "restricted": profile(restricted, restricted_platform), "root": profile(w.root, w.root.tokens)}

    @app.get("/__test__/seed")
    def seeded():
        return seed

    uvicorn.run(app, host="0.0.0.0", port=18767, access_log=False)


if __name__ == "__main__":
    main()
