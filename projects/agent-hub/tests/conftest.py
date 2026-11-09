import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from agent_hub.api import create_app
from agent_hub.config import Settings, get_settings
from agent_hub.db import coordinate
from agent_hub.mail import Mailer
from agent_hub.models import Base
from agent_hub.security import b64, public_key

PASSWORD = "test-password-for-hub-2026"
SETUP_TOKEN = "test-bootstrap-token-" + "a" * 40


class DeviceKey:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))

    def proof(self, method, path, token=None, **overrides):
        claims = {
            "htm": method.upper(),
            "htu": "http://testserver" + path.split("?")[0],
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
        }
        if token:
            claims["ath"] = b64(hashlib.sha256(token.encode()).digest())
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": self.jwk})


@dataclass
class Identity:
    user_id: str
    key: DeviceKey
    enrollment: dict
    tokens: dict | None = None

    @property
    def token(self):
        return self.tokens["access_token"]


@pytest.fixture(scope="session")
def database():
    owner_url, runtime_url = os.getenv("HUB_TEST_OWNER_URL"), os.getenv("HUB_TEST_RUNTIME_URL")
    if not owner_url or not runtime_url:
        pytest.fail("Real PostgreSQL is required: set HUB_TEST_OWNER_URL and HUB_TEST_RUNTIME_URL")
    # This suite truncates test data. Never allow a normal application database.
    if any(make_url(url).database != "hub_test" for url in (owner_url, runtime_url)):
        pytest.fail("Integration tests only operate on a database named hub_test")
    os.environ["HUB_DATABASE_URL"] = owner_url
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    cfg = Settings(
        database_url=runtime_url,
        public_url="http://testserver",
        environment="test",
        mail_mode="memory",
        bootstrap_token=SETUP_TOKEN,
    )
    engine = create_engine(owner_url, hide_parameters=True)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield cfg, factory
    engine.dispose()


class World:
    def __init__(self, client, mail, owner):
        self.client, self.mail, self.owner = client, mail, owner
        self.root = None

    def call(
        self, method, path, *, who=None, token=None, key=None, body=None, idem=None, proof=None, setup=None
    ):
        if who:
            key, token = who.key, who.token
        if setup:
            token = setup
        headers = {}
        if key:
            headers["DPoP"] = proof or key.proof(method, path, token)
        if token:
            headers["X-Setup-Token" if setup else "Authorization"] = token if setup else f"DPoP {token}"
        if method != "GET":
            headers["Idempotency-Key"] = idem or str(uuid.uuid4())
        return self.client.request(method, path, headers=headers, json=body)

    def ok(self, method, path, **kwargs):
        response = self.call(method, path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def enroll(self, login, key=None):
        key = key or DeviceKey()
        result = self.ok(
            "POST",
            "/api/auth/enrollments",
            key=key,
            body={"login": login, "password": PASSWORD, "device_name": "test-device", "os": "windows"},
        )
        return key, result

    def email(self, identity):
        args = {"key": identity.key, "token": identity.enrollment["enrollment_token"]}
        challenge = self.ok("POST", "/api/devices/email-challenges", body={}, **args)
        self.ok(
            "POST",
            "/api/devices/email-verifications",
            body={"challenge_id": challenge["challenge_id"], "code": self.mail.messages[-1]["code"]},
            **args,
        )
        return challenge

    def decide(self, approval, approve=True):
        return self.ok(
            "POST",
            f"/api/admin/approvals/{approval['id']}/decision",
            who=self.root,
            body={
                "approve": approve,
                "expected_version": approval["version"],
                "expected_hash": approval["payload_hash"],
            },
        )

    def start(self, identity, grant=None):
        identity.tokens = self.ok(
            "POST",
            "/api/auth/sessions",
            key=identity.key,
            token=identity.enrollment["enrollment_token"],
            body={"grant_id": grant} if grant else {},
        )
        return identity

    def project(self, slug="project-a"):
        return self.ok("POST", "/api/admin/projects", who=self.root, body={"slug": slug, "name": slug})["id"]

    def member(self, project, login="member-one", email=None, approve=True):
        user = {"login": login, "password": PASSWORD, "email": email}
        uid = self.ok(
            "POST", f"/api/projects/{project}/member-invitations", who=self.root, body={"new_user": user}
        )["user"]["id"]
        key, enrollment = self.enroll(login)
        identity = Identity(uid, key, enrollment)
        if email:
            self.email(identity)
        if approve:
            self.decide(enrollment["approval"])
            self.start(identity)
        return identity

    def grant(self, identity, project, caps=None, requester=None, approve=True):
        a = self.ok(
            "POST",
            "/api/grant-requests",
            who=requester or self.root,
            body={
                "project_id": project,
                "user_id": identity.user_id,
                "device_id": identity.enrollment["device_id"],
                "capabilities": caps or ["project:read"],
            },
        )
        return self.decide(a)["grant_id"] if approve else a

    def administrator(self, project, login="admin-one"):
        user = self.ok(
            "POST",
            "/api/admin/users/admins",
            who=self.root,
            body={"login": login, "password": PASSWORD, "email": f"{login}@example.org"},
        )
        key, enrollment = self.enroll(login)
        identity = Identity(user["id"], key, enrollment)
        self.email(identity)
        self.decide(enrollment["approval"])
        self.ok(
            "PUT",
            f"/api/admin/projects/{project}/administrators",
            who=self.root,
            body={"user_id": user["id"]},
        )
        grant = self.grant(
            identity, project, ["project:read", "members:manage", "grants:request", "audit:read"]
        )
        return self.start(identity, grant)


@pytest.fixture
def empty_world(database, tmp_path):
    cfg, owner = database
    cfg = cfg.model_copy(update={"artifact_directory": str(tmp_path / "work-archives")})
    with owner.begin() as db:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE TABLE {names} CASCADE"))
    mail = Mailer(cfg)
    app = create_app(cfg, mailer=mail)
    with TestClient(app) as client:
        yield World(client, mail, owner)
    app.state.sessions.kw["bind"].dispose()


@pytest.fixture
def world(empty_world):
    w = empty_world
    root_key = DeviceKey()
    with w.owner.begin() as db:
        coordinate(db)
        _, thumb = public_key(root_key.jwk)
        user, _ = w.client.app.state.hub.bootstrap(
            db, "root-admin", PASSWORD, "root@example.org", root_key.jwk, thumb, "windows"
        )
        uid = str(user.id)
    _, enrollment = w.enroll("root-admin", root_key)
    w.root = w.start(Identity(uid, root_key, enrollment))
    return w
