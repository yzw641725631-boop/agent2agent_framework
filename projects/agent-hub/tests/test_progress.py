import uuid

from sqlalchemy import select

from agent_hub.db import context, session_factory
from agent_hub.models import ProjectProgress


def test_progress_is_durable_idempotent_and_restricted(world):
    w = world
    project, other = w.project(), w.project("other-project")
    member = w.member(project)
    w.start(member, w.grant(member, project, ["project:read", "tasks:write"]))
    path = f"/api/projects/{project}/progress"
    report = {"summary": "完成接口实现", "status": "checkpoint", "task_reference": "API-1"}
    key = str(uuid.uuid4())
    first = w.ok("POST", path, who=member, body=report, idem=key)
    assert w.ok("POST", path, who=member, body=report, idem=key) == first
    assert w.ok("GET", path, who=member)["items"] == [first]
    assert w.ok("GET", path + f"?after={first['sequence']}", who=member)["items"] == []
    assert w.call("GET", f"/api/projects/{other}/progress", who=member).status_code == 404
    assert w.call("POST", f"/api/projects/{other}/progress", who=member, body=report).status_code == 404
    w.ok("POST", f"/api/projects/{project}/members/{member.user_id}/revoke", who=w.root, body={})
    assert w.call("GET", path, who=member).json()["code"] == "PROJECT_ACCESS_REVOKED"
    assert w.call("POST", path, who=member, body=report).json()["code"] == "PROJECT_ACCESS_REVOKED"


def test_progress_read_only_cannot_report_and_rls_denies_without_context(world, database):
    w = world
    project = w.project()
    w.ok("POST", f"/api/projects/{project}/progress", who=w.root, body={"summary": "设计完成"})
    reader = w.member(project)
    grant = w.grant(reader, project)
    w.start(reader, grant)
    path = f"/api/projects/{project}/progress"
    assert len(w.ok("GET", path, who=reader)["items"]) == 1
    assert w.call("POST", path, who=reader, body={"summary": "not allowed"}).status_code == 404
    runtime = session_factory(database[0])
    with runtime() as db:
        assert list(db.scalars(select(ProjectProgress))) == []
        context(db, reader.user_id, reader.enrollment["device_id"], grant)
        assert len(list(db.scalars(select(ProjectProgress)))) == 1


def test_project_sessions_preserve_other_sessions_and_reject_other_grants(world):
    w = world
    project, other = w.project(), w.project("project-b")
    member = w.member(project)
    first_grant = w.grant(member, project)
    # Add same user to a second project and grant that device independently.
    w.ok("POST", f"/api/projects/{other}/member-invitations", who=w.root,
         body={"user_id": member.user_id})
    second_grant = w.grant(member, other)
    original = member.token
    one = w.ok("POST", "/api/auth/project-sessions", who=member, body={"grant_id": first_grant})
    two = w.ok("POST", "/api/auth/project-sessions", who=member, body={"grant_id": second_grant})
    assert w.ok("GET", "/api/me", key=member.key, token=original)["grant_id"] is None
    for project_id, session in [(project, one), (other, two)]:
        assert w.ok("GET", f"/api/projects/{project_id}/context", key=member.key,
                    token=session["access_token"])["id"] == project_id
    stranger = w.member(project, login="another-member")
    assert w.call("POST", "/api/auth/project-sessions", who=stranger,
                  body={"grant_id": first_grant}).status_code == 404


def test_progress_pagination_validation_and_stream_authorization(world):
    w = world
    project = w.project()
    path = f"/api/projects/{project}/progress"
    for summary in ["first", "second", "third"]:
        w.ok("POST", path, who=w.root, body={"summary": summary})
    first = w.ok("GET", path + "?limit=2", who=w.root)
    assert [e["summary"] for e in first["items"]] == ["first", "second"]
    assert first["has_more"] is True
    last = w.ok("GET", path + f"?after={first['cursor']}&limit=2", who=w.root)
    assert [e["summary"] for e in last["items"]] == ["third"]
    for bad in [{"summary": " "}, {"summary": "x", "status": "unknown"},
                {"summary": "x", "git_commit": "../../secret"}]:
        assert w.call("POST", path, who=w.root, body=bad).status_code == 422
    assert w.call("GET", path + "/stream").status_code == 401
