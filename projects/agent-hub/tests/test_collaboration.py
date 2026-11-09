import uuid

from test_agent_dispatch import register
from test_tasks import claim, developer, gate, lease, new_task, ready
from test_work_policy import observed_body, prepare


def settings(w, project, **changes):
    path = f"/api/projects/{project}/collaboration"
    previous = w.ok("GET", path, who=w.root)
    return w.ok(
        "PUT",
        path,
        who=w.root,
        body={
            "expected_version": previous["version"],
            "profiles": previous["profiles"],
            "allow_compact": previous["allow_compact"],
            "change_reason": "明确项目岗位与执行流程的实际要求",
            **changes,
        },
    )


def profile():
    return {
        "key": "web-specialist",
        "name": "Web 前端工程师",
        "base_role": "developer",
        "enabled": True,
        "responsibilities": ["仅开发已授权 Web 页面，遵守任务范围。"],
        "outputs": ["页面与交接说明"],
    }


def test_custom_role_invalidates_preparation_and_never_grants_rights(world):
    w, p = world, world.project()
    member = developer(w, p)
    settings(w, p, profiles=[profile()])
    task = ready(w, p, role_profile="web-specialist", execution_mode="observed")
    claimed = claim(w, p, task, member)
    context = prepare(w, p, claimed, member)
    assert context["payload"]["role"]["id"] == "developer"
    assert context["payload"]["role"]["name"] == "Web 前端工程师"
    assert context["payload"]["role"]["grants_permissions"] is False
    changed = profile()
    changed["responsibilities"].append("完成键盘可访问性验证。")
    settings(w, p, profiles=[changed])
    base = f"/api/projects/{p}/tasks/{task['id']}"
    assert (
        w.call("POST", base + "/worklogs", who=member, body=observed_body(claimed, context)).json()["code"]
        == "WORK_ROLE_STALE"
    )
    updated = prepare(w, p, claimed, member)
    w.ok("POST", base + "/worklogs", who=member, body=observed_body(claimed, updated))
    assert (
        w.call(
            "PUT",
            f"/api/projects/{p}/collaboration",
            who=member,
            body={"expected_version": 2, "profiles": [], "change_reason": "不能由岗位说明获得设置项目的权限"},
        ).status_code
        == 404
    )
    other = w.project("other-project")
    assert w.call("GET", f"/api/projects/{other}/collaboration", who=member).status_code == 404


def test_role_profile_cannot_change_base_or_be_used_for_unrelated_dispatch(world):
    w, p = world, world.project()
    settings(w, p, profiles=[profile()])
    task = ready(w, p, role_profile="web-specialist")
    endpoint = register(w, p)
    path = f"/api/projects/{p}/agent-dispatches"
    body = {
        "task_id": task["id"],
        "endpoint_id": endpoint["id"],
        "expected_task_version": task["version"],
        "expected_endpoint_version": endpoint["version"],
        "note": "请交给具有对应岗位登记的真实执行者",
    }
    assert w.call("POST", path, who=w.root, body=body).json()["code"] == "AGENT_ROLE_MISMATCH"
    endpoint = w.ok(
        "POST",
        f"/api/projects/{p}/agents",
        who=w.root,
        body={
            "instance_id": endpoint["instance_id"],
            "harness": "codex",
            "name": endpoint["name"],
            "roles": ["developer"],
            "role_profiles": ["web-specialist"],
            "expected_version": endpoint["version"],
        },
    )
    w.ok("POST", path, who=w.root, body={**body, "expected_endpoint_version": endpoint["version"]})
    changed = profile()
    changed["base_role"] = "architect"
    assert (
        w.call(
            "PUT",
            f"/api/projects/{p}/collaboration",
            who=w.root,
            body={
                "expected_version": 1,
                "profiles": [changed],
                "change_reason": "不能改变同一岗位标识对应的基础职业",
            },
        ).json()["code"]
        == "ROLE_PROFILE_BASE_FROZEN"
    )
    settings(w, p, profiles=[])
    assert (
        w.call(
            "PUT",
            f"/api/projects/{p}/collaboration",
            who=w.root,
            body={
                "expected_version": 2,
                "profiles": [changed],
                "change_reason": "删除岗位后也不允许改变历史基础职业",
            },
        ).json()["code"]
        == "ROLE_PROFILE_BASE_FROZEN"
    )


def test_compact_is_explicit_opt_in_and_preserves_post_submission_gates(world):
    w, p = world, world.project()
    body = {
        "title": "小型文档修正",
        "description": "文档修正",
        "acceptance": "文字清晰",
        "execution_mode": "declaration",
        "scope": {"repository": "web", "platform": "web", "paths": ["src"]},
        "workflow": "compact",
        "workflow_reason": "仅文档文字调整，省略单独设计会议",
    }
    assert (
        w.call("POST", f"/api/projects/{p}/tasks", who=w.root, body=body).json()["code"]
        == "COMPACT_WORKFLOW_DISABLED"
    )
    settings(w, p, allow_compact=True)
    task = new_task(w, p, **body)
    task = gate(w, p, task, "requirements")
    assert (
        w.call(
            "POST",
            f"/api/projects/{p}/tasks/{task['id']}/decisions",
            who=w.root,
            body={
                "gate": "design",
                "approved": True,
                "expected_version": task["version"],
                "evidence": "应按已批准流程进行",
                "instance_id": str(uuid.uuid4()),
                "harness": "human",
            },
        ).json()["code"]
        == "TASK_GATE_ORDER_INVALID"
    )
    task = gate(w, p, task, "feasibility")
    assert task["status"] == "ready" and task["workflow_policy"]["post_submission"] == [
        "review",
        "test",
        "release",
    ]
    settings(w, p, allow_compact=False)
    claimed = claim(w, p, task)
    assert claimed["task"]["workflow"] == "compact"  # Existing explicit decisions remain pinned.


def test_workflow_change_requires_handoff_and_only_upgrades_executed_tasks(world):
    w, p = world, world.project()
    settings(w, p, allow_compact=True)
    task = new_task(w, p, workflow="compact", workflow_reason="此次是已明确确认的低复杂度文档任务")
    task = gate(w, p, gate(w, p, task, "requirements"), "feasibility")
    claimed = claim(w, p, task)
    path = f"/api/projects/{p}/tasks/{task['id']}"
    body = {
        "expected_version": claimed["task"]["version"],
        "workflow": "standard",
        "reason": "发现接口影响，需要升级并重新完成设计准入",
    }
    assert (
        w.call("POST", path + "/workflow", who=w.root, body=body).json()["code"]
        == "WORKFLOW_CHANGE_REQUIRES_RELEASE"
    )
    released = w.ok(
        "POST", path + "/release", who=w.root, body={**lease(claimed), "reason": "保留工作后交接以升级流程"}
    )
    upgraded = w.ok(
        "POST", path + "/workflow", who=w.root, body={**body, "expected_version": released["version"]}
    )
    assert upgraded["status"] == "draft" and upgraded["admission_step"] == 0
    assert len(w.ok("GET", path, who=w.root)["decisions"]) == 2
    assert (
        w.call(
            "POST",
            path + "/workflow",
            who=w.root,
            body={**body, "expected_version": upgraded["version"], "workflow": "compact"},
        ).json()["code"]
        == "WORKFLOW_DOWNGRADE_FORBIDDEN"
    )


def test_strict_requires_real_repository_and_code_mode(world):
    w, p = world, world.project()
    assert (
        w.call(
            "POST",
            f"/api/projects/{p}/tasks",
            who=w.root,
            body={
                "title": "strict",
                "description": "strict",
                "acceptance": "verified code",
                "workflow": "strict",
                "workflow_reason": "此发布必须使用可核验的代码仓库交付证据",
                "execution_mode": "declaration",
                "scope": {"repository": "web", "platform": "web", "paths": ["src"]},
            },
        ).json()["code"]
        == "WORKFLOW_CODE_REPOSITORY_REQUIRED"
    )
