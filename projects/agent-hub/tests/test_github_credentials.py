import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from test_repositories import grant, register

from agent_hub import github_credentials
from agent_hub.db import context
from agent_hub.models import ProjectRepository, SiteConfiguration


def test_github_app_keys_are_encrypted_and_installation_tokens_are_repository_and_permission_scoped(world):
    w, p = world, world.project()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    route = "/api/admin/integrations/github"
    assert w.ok("GET", route, who=w.root) == {"version": 0, "app_id": None, "private_key_set": False}
    configured = w.ok(
        "PUT", route, who=w.root, body={"expected_version": 0, "app_id": 1234, "private_key": pem}
    )
    assert configured == {"version": 1, "app_id": 1234, "private_key_set": True}
    assert "PRIVATE KEY" not in json.dumps(configured)
    repo = register(w, p, auth_mode="github_app", token=None, installation_id=4567)
    member = w.member(p)
    w.start(member, grant(w, member, p, [(repo, ["code:read"])]))
    assert w.call("GET", route, who=member).status_code == 403
    assert w.call("PUT", route, who=member, body={"expected_version": 1, "app_id": 1234}).status_code == 403
    assert w.call("PUT", route, who=w.root, body={"expected_version": 0, "app_id": 1234}).status_code == 409
    assert w.call("PUT", route, who=w.root, body={"expected_version": 1, "app_id": 9999}).status_code == 422
    assert (
        w.call(
            "PUT", route, who=w.root, body={"expected_version": 1, "app_id": 1234, "private_key": "not-a-key"}
        ).status_code
        == 422
    )
    w.ok("PUT", route, who=w.root, body={"expected_version": 1, "app_id": 1234})
    calls = []

    def exchange(request):
        assert str(request.url) == "https://api.github.com/app/installations/4567/access_tokens"
        claims = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            private.public_key(),
            algorithms=["RS256"],
        )
        assert claims["iss"] == "1234" and 0 < claims["exp"] - claims["iat"] <= 600
        body = json.loads(request.content)
        assert body["repositories"] == ["web"]
        assert body["permissions"] in (
            {"contents": "read"},
            {"contents": "write"},
            {"contents": "read", "actions": "read"},
        )
        calls.append(body)
        return httpx.Response(201, json={"token": "short-lived-installation-token"})

    with w.owner() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        encrypted = db.get(SiteConfiguration, "github-app").encrypted_value
        assert pem not in encrypted and "PRIVATE KEY" not in encrypted
        stored = db.scalar(select(ProjectRepository).where(ProjectRepository.id == repo["id"]))
        transport = httpx.MockTransport(exchange)
        assert (
            github_credentials.credential(w.client.app.state.hub, db, stored, transport=transport)
            == "short-lived-installation-token"
        )
        github_credentials.credential(w.client.app.state.hub, db, stored, write=True, transport=transport)
        material = github_credentials.credential_material(
            w.client.app.state.hub, db, stored, read_permissions=["actions"]
        )
        github_credentials.exchange_credential(material, transport=transport)
    assert [call["permissions"] for call in calls] == [
        {"contents": "read"},
        {"contents": "write"},
        {"contents": "read", "actions": "read"},
    ]
