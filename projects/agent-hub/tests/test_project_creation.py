from sqlalchemy import select

from agent_hub.db import context
from agent_hub.models import Project


def test_root_project_creation_retries_are_idempotent_and_members_cannot_create(world):
    w = world
    args = {
        "who": w.root,
        "body": {"slug": "created-by-agent", "name": "Agent 项目"},
        "idem": "create-project-stable-operation",
    }
    first = w.ok("POST", "/api/admin/projects", **args)
    assert w.ok("POST", "/api/admin/projects", **args) == first
    conflict = w.call(
        "POST",
        "/api/admin/projects",
        who=w.root,
        body={"slug": "created-by-agent", "name": "Other operation"},
    )
    assert conflict.json()["code"] == "PROJECT_ALREADY_EXISTS"
    member = w.member(first["id"])
    forbidden = w.call(
        "POST", "/api/admin/projects", who=member, body={"slug": "forbidden-agent", "name": "Forbidden"}
    )
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "SUPERADMIN_REQUIRED"
    with w.owner() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        assert len(list(db.scalars(select(Project).where(Project.slug == "created-by-agent")))) == 1
