import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from test_repositories import grant, register
from test_repositories import task as repository_task
from test_tasks import claim, developer, gate, lease, new_task, ready, submit

from agent_hub.db import context, session_factory
from agent_hub.models import Task, TaskDependency
from agent_hub.security import now


def update(w, p, t, ids, **kwargs):
    return w.call("POST", f"/api/projects/{p}/tasks/{t['id']}/dependencies", who=kwargs.pop("who", w.root),
                  body={"expected_version": t["version"], "depends_on": ids,
                        "reason": "按实际接口交付顺序调整前置关系"}, **kwargs)


def attempt(w, p, t, who=None):
    return w.call("POST", f"/api/projects/{p}/tasks/{t['id']}/claim", who=who or w.root,
                  body={"instance_id": str(uuid.uuid4()), "harness": "codex"})


def finish(w, p, t):
    t, _ = submit(w, p, claim(w, p, t))
    for name in ("review", "test", "release"):
        t = gate(w, p, t, name, override_reason="隔离测试中超级管理员明确自审" if name != "release" else "")
    return t


def test_dependency_blocks_until_actual_completion_then_freezes(world):
    w, p = world, world.project()
    first = ready(w, p)
    second = ready(w, p, depends_on=[first["id"]])
    base = f"/api/projects/{p}/tasks/{second['id']}"
    before = w.ok("GET", base, who=w.root)["coordination"]
    assert not before["dependencies_satisfied"] and not before["claimable"]
    assert before["dependencies"][0]["id"] == first["id"]
    assert attempt(w, p, second).json()["code"] == "TASK_DEPENDENCIES_INCOMPLETE"
    w.ok("POST", f"/api/projects/{p}/progress", who=w.root,
         body={"summary": "narrative complete", "status": "completed", "task_reference": first["id"]})
    assert attempt(w, p, second).json()["code"] == "TASK_DEPENDENCIES_INCOMPLETE"
    finish(w, p, first)
    assert w.ok("GET", base, who=w.root)["coordination"]["claimable"]
    claimed = claim(w, p, second)
    assert update(w, p, claimed["task"], []).json()["code"] == "TASK_DEPENDENCIES_FROZEN"


def test_updates_restart_admission_check_version_and_reject_cycles(world):
    w, p = world, world.project()
    a, b, c = ready(w, p), ready(w, p), ready(w, p)
    a = update(w, p, a, [b["id"]]).json()
    assert a["status"] == "draft" and a["admission_step"] == 0
    assert update(w, p, {**a, "version": 1}, []).json()["code"] == "TASK_VERSION_CONFLICT"
    b = update(w, p, b, [c["id"]]).json()
    assert update(w, p, c, [a["id"]]).json()["code"] == "TASK_DEPENDENCY_CYCLE"
    assert update(w, p, c, [c["id"]]).json()["code"] == "TASK_DEPENDENCY_CYCLE"
    assert update(w, p, c, [a["id"], a["id"]]).json()["code"] == "TASK_DEPENDENCY_DUPLICATE"
    assert update(w, p, c, [str(uuid.uuid4())]).json()["code"] == "TASK_DEPENDENCY_NOT_ACCESSIBLE"
    key = str(uuid.uuid4())
    response = update(w, p, c, [], idem=key)
    assert response.status_code == 200
    assert update(w, p, c, [], idem=key).json() == response.json()


def test_implicit_parent_edges_cannot_deadlock_explicit_dependencies(world):
    w, p = world, world.project()
    parent = new_task(w, p)
    child = new_task(w, p, parent_id=parent["id"])
    assert update(w, p, child, [parent["id"]]).json()["code"] == "TASK_DEPENDENCY_CYCLE"
    other = new_task(w, p, depends_on=[parent["id"]])
    assert update(w, p, child, [other["id"]]).json()["code"] == "TASK_DEPENDENCY_CYCLE"


def test_concurrent_opposing_dependencies_cannot_create_cycle(world):
    w, p = world, world.project()
    a, b = new_task(w, p), new_task(w, p)
    member = w.administrator(p)
    w.start(member, w.grant(member, p, ["project:read", "tasks:publish"]))
    barrier = Barrier(2)

    def compete(t, dependency, who):
        barrier.wait(timeout=5)
        return update(w, p, t, [dependency["id"]], who=who)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(compete, a, b, w.root), pool.submit(compete, b, a, member)]
        responses = [f.result(timeout=15) for f in futures]
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()["code"] == "TASK_DEPENDENCY_CYCLE"


def test_cross_repository_dependencies_hide_ungranted_sources_and_cannot_be_erased(world, database):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    hidden = repository_task(w, p, pc, title="private prerequisite design")
    visible = repository_task(w, p, web, depends_on=[hidden["id"]])
    member = w.administrator(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write"])],
                          caps=["project:read", "tasks:claim", "tasks:write", "tasks:publish"]))
    result = w.ok("GET", f"/api/projects/{p}/tasks/{visible['id']}", who=member)
    assert result["coordination"]["has_restricted_dependencies"]
    assert not result["coordination"]["dependencies_satisfied"]
    assert hidden["id"] not in json.dumps(result) and hidden["title"] not in json.dumps(result)
    assert attempt(w, p, visible, member).json()["code"] == "TASK_DEPENDENCIES_INCOMPLETE"
    assert update(w, p, visible, [], who=member).json()["code"] == "TASK_DEPENDENCIES_RESTRICTED"
    assert hidden["id"] not in json.dumps(w.ok("GET", f"/api/projects/{p}/tasks", who=member))
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
            assert not list(db.scalars(select(TaskDependency)))
            assert not db.scalar(text("SELECT hub.task_dependencies_satisfied(:pid,:tid)"), {"pid": p, "tid": hidden["id"]})
    finally:
        runtime.kw["bind"].dispose()
    finish(w, p, hidden)
    assert w.ok("GET", f"/api/projects/{p}/tasks/{visible['id']}", who=member)["coordination"]["dependencies_satisfied"]
    assert claim(w, p, visible, member)["run"]["user_id"] == member.user_id


def test_parent_cannot_release_while_unreadable_child_is_incomplete(world):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    parent = repository_task(w, p, web)
    repository_task(w, p, pc, parent_id=parent["id"])
    t, _ = submit(w, p, claim(w, p, parent))
    for name in ("review", "test"):
        t = gate(w, p, t, name, override_reason="隔离测试中超级管理员明确自审")
    member = w.administrator(p)
    w.start(member, grant(w, member, p, [(web, ["code:read"])],
                          caps=["project:read", "tasks:publish", "logs:read"]))
    response = w.call("POST", f"/api/projects/{p}/tasks/{t['id']}/decisions", who=member, body={
        "gate": "release", "approved": True, "expected_version": t["version"],
        "worklog_id": t["submission_id"], "evidence": "验证不可见子任务仍然阻止发布",
        "instance_id": str(uuid.uuid4()), "harness": "human"})
    assert response.json()["code"] == "TASK_CHILDREN_INCOMPLETE"


def test_cross_project_dependencies_are_rejected(world):
    w, p, other = world, world.project(), world.project("other")
    a, b = new_task(w, p), new_task(w, other)
    assert update(w, p, a, [b["id"]]).json()["code"] == "TASK_DEPENDENCY_NOT_ACCESSIBLE"


def code_task(w, p, paths, **kwargs):
    return ready(w, p, execution_mode="observed", scope={"repository": "web", "platform": "web", "paths": paths}, **kwargs)


def test_concurrent_overlapping_tasks_allow_only_one_writer(world):
    w, p = world, world.project()
    a, b = code_task(w, p, ["src/UI"]), code_task(w, p, ["Src/ui/login.ts"])
    member = developer(w, p)
    barrier = Barrier(2)

    def compete(t, who):
        barrier.wait(timeout=5)
        return attempt(w, p, t, who)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(compete, [a, b], [w.root, member]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()["code"] == "TASK_SCOPE_CONFLICT"


def test_disjoint_paths_and_repositories_can_run_and_release_reopens_scope(world):
    w, p = world, world.project()
    a = code_task(w, p, ["src/web"])
    b = code_task(w, p, ["src/api"])
    c = code_task(w, p, ["src/web/login.ts"])
    first = claim(w, p, a)
    claim(w, p, b)
    assert attempt(w, p, c).json()["code"] == "TASK_SCOPE_CONFLICT"
    coordination = w.ok("GET", f"/api/projects/{p}/tasks/{c['id']}", who=w.root)["coordination"]
    assert coordination["scope_conflicts"][0]["task_id"] == a["id"]
    w.ok("POST", f"/api/projects/{p}/tasks/{a['id']}/release", who=w.root,
         body={**lease(first), "reason": "handoff"})
    claim(w, p, c)


def test_expired_lease_frees_scope_and_review_retains_it(world):
    w, p = world, world.project()
    a, b = code_task(w, p, ["src"]), code_task(w, p, ["src"])
    claim(w, p, a)
    with w.owner.begin() as db:
        db.get(Task, uuid.UUID(a["id"])).lease_until = now() - timedelta(seconds=1)
    claim(w, p, b)
    with w.owner.begin() as db:
        t = db.get(Task, uuid.UUID(b["id"]))
        t.status, t.active_run_id, t.lease_until = "review", None, None
    assert attempt(w, p, a).json()["code"] == "TASK_SCOPE_CONFLICT"


@pytest.mark.parametrize("value", ["src/space ", "src/dot.", "C:/src", "src/e\u0301"])
def test_code_scopes_use_cross_platform_stable_names(world, value):
    w, p = world, world.project()
    response = w.call("POST", f"/api/projects/{p}/tasks", who=w.root, body={
        "title": "unstable path", "description": "reject", "acceptance": "reject",
        "scope": {"repository": "web", "platform": "web", "paths": [value]}})
    assert response.status_code == 422
