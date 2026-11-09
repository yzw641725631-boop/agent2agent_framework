import uuid

from test_tasks import claim, gate, lease, log_body, ready, submit

CONTENT = {
    "title": "登录页面使用说明",
    "product_version": "1.0.0",
    "user_roles": ["普通成员"],
    "purpose": "帮助已获准的项目成员完成登录并检查设备验证状态。",
    "prerequisites": "已经获得账号和本人设备批准。",
    "entry": "打开网站的登录页面。",
    "steps": ["填写登录名及密码。", "点击登录，在绑定邮箱中查收验证码并填写。"],
    "inputs": "密码至少十二个字符；验证码八位数字。",
    "results": "成功后显示项目目录。",
    "recovery": "验证码失效时重新发送；账号冻结请联系管理员。",
    "cautions": "不要向他人提供密码或验证码。",
    "examples": "使用已批准的测试账号登录并确认目录可见。",
    "changes": "首次发布。",
    "limitations": "没有邮箱的成员须由超管审批设备。",
    "verification": "已依据冻结版本检查表单入口；截图环境尚待产品人员核对。",
}


def fixture(w):
    p = w.project()
    task = ready(w, p, documentation={"kind": "user", "module": "登录与设备验证"})
    run = claim(w, p, task)
    submitted, log = submit(w, p, run)
    assert submitted["guide_generation"]["items"][0]["instance_id"] == run["run"]["instance_id"]
    assert submitted["documentation"]["kind"] == "user"
    assert log["outcome"] == "checkpoint"
    jobs = w.ok("GET", f"/api/projects/{p}/guides", who=w.root)["items"]
    assert len(jobs) == 1
    return p, run, submitted, log, jobs[0]


def generate(w, p, job):
    base = f"/api/projects/{p}/guides/{job['id']}"
    claimed = w.ok(
        "POST",
        base + "/claim",
        who=w.root,
        body={"expected_version": job["version"], "instance_id": job["instance_id"]},
    )
    return w.ok(
        "POST",
        base + "/complete",
        who=w.root,
        body={
            "expected_version": claimed["version"],
            "instance_id": job["instance_id"],
            "lease_epoch": claimed["lease_epoch"],
            "source_hash": job["source_hash"],
            "content": CONTENT,
        },
    )


def approve(w, p, job, audience="repository"):
    path = f"/api/projects/{p}/guides/{job['id']}/decisions"
    for review_gate in ["product", "test"]:
        job = w.ok(
            "POST",
            path,
            who=w.root,
            body={
                "expected_version": job["version"],
                "draft_version": job["draft_version"],
                "gate": review_gate,
                "approved": True,
                "audience": audience,
                "reason": "已按冻结实现逐项核对使用流程和可执行的操作步骤。",
                "override_reason": "独立测试环境由超管明确记录自审例外；正式流程仍要求独立人员。",
            },
        )
    return job


def test_submit_freezes_contributors_and_guide_lease_is_independent_of_development(world):
    w = world
    p, run, task, log, job = fixture(w)
    assert job["source_worklog_id"] == log["id"] and job["source_hash"] == log["receipt_hash"]
    assert job["instance_id"] == run["run"]["instance_id"] and task["active_run_id"] is None
    value = w.ok("GET", f"/api/projects/{p}/guides/{job['id']}/input", who=w.root)
    assert value["worklog"]["receipt_hash"] == log["receipt_hash"]
    assert value["output_schema"]["required"]
    wrong = w.call(
        "POST",
        f"/api/projects/{p}/guides/{job['id']}/claim",
        who=w.root,
        body={"expected_version": job["version"], "instance_id": str(uuid.uuid4())},
    )
    assert wrong.json()["code"] == "GUIDE_TARGET_MISMATCH"
    result = generate(w, p, job)
    assert result["state"] == "review" and result["content"] == CONTENT
    path = f"/api/projects/{p}/guides/{job['id']}/decisions"
    assert (
        w.call(
            "POST",
            path,
            who=w.root,
            body={
                "expected_version": result["version"],
                "draft_version": 1,
                "gate": "product",
                "approved": True,
                "reason": "作者不能无例外批准本人提交的使用说明。",
            },
        ).json()["code"]
        == "GUIDE_INDEPENDENT_REVIEW_REQUIRED"
    )


def test_release_requires_all_guide_reviews_and_publishes_only_the_frozen_revision(world):
    w = world
    p, _, task, _, job = fixture(w)
    task = gate(w, p, task, "review", override_reason="测试环境中明确记录超管自审例外。")
    task = gate(w, p, task, "test", override_reason="测试环境中明确记录超管自测例外。")
    blocked = w.call(
        "POST",
        f"/api/projects/{p}/tasks/{task['id']}/decisions",
        who=w.root,
        body={
            "gate": "release",
            "approved": True,
            "expected_version": task["version"],
            "worklog_id": task["submission_id"],
            "evidence": "等待使用说明审批完成。",
            "instance_id": str(uuid.uuid4()),
            "harness": "human",
        },
    )
    assert blocked.json()["code"] == "GUIDES_NOT_APPROVED"
    approved = approve(w, p, generate(w, p, job), "project")
    assert approved["state"] == "approved"
    completed = gate(w, p, task, "release")
    assert completed["status"] == "completed"
    published = w.ok("GET", f"/api/projects/{p}/guides/{job['id']}", who=w.root)
    assert published["state"] == "published" and published["published_at"]
    assert published["content_hash"] == approved["content_hash"]


def test_handoff_contributors_each_get_a_job_and_resubmission_reuses_unchanged_sources(world):
    w = world
    p = w.project()
    task = ready(w, p, documentation={"kind": "user", "module": "多人共同实现的页面"})
    first = claim(w, p, task)
    base = f"/api/projects/{p}/tasks/{task['id']}"
    w.ok("POST", base + "/worklogs", who=w.root, body=log_body(first))
    w.ok(
        "POST",
        base + "/release",
        who=w.root,
        body={**lease(first), "reason": "交给另一个执行实例继续完成界面开发。"},
    )
    second = claim(w, p, w.ok("GET", base, who=w.root)["task"])
    task, log = submit(w, p, second)
    jobs = w.ok("GET", f"/api/projects/{p}/guides", who=w.root)["items"]
    assert len(jobs) == 2 and len({j["instance_id"] for j in jobs}) == 2
    assert {j["source_worklog_id"] for j in jobs} >= {log["id"]}
    assert (
        w.ok("GET", f"/api/projects/{p}/guides?instance_id={first['run']['instance_id']}", who=w.root)[
            "items"
        ][0]["instance_id"]
        == first["run"]["instance_id"]
    )


def test_guide_reader_without_code_permission_cannot_see_drafts_sources_or_private_events(world):
    from sqlalchemy import text
    from test_repositories import grant, register
    from test_repositories import task as repository_task

    w, p = world, world.project()
    repo = register(w, p)
    # Declaration mode exercises the guide authority boundaries without creating a synthetic code archive.
    created = repository_task(w, p, repo, documentation={"kind": "user", "module": "私有实现的使用说明"})
    run = claim(w, p, created)
    task, _ = submit(w, p, run)
    job = w.ok("GET", f"/api/projects/{p}/guides", who=w.root)["items"][0]
    reader = w.member(p, "guide-reader")
    w.start(reader, grant(w, reader, p, [], caps=["project:read", "guides:read"]))
    path = f"/api/projects/{p}/guides"
    assert w.ok("GET", path, who=reader)["items"] == []
    assert w.call("GET", path + f"/{job['id']}", who=reader).status_code == 404
    approve(w, p, generate(w, p, job), "project")
    task = gate(w, p, task, "review", override_reason="超管在隔离测试中记录自审例外原因。")
    task = gate(w, p, task, "test", override_reason="超管在隔离测试中记录自测例外原因。")
    gate(w, p, task, "release")
    result = w.ok("GET", path + f"/{job['id']}", who=reader)
    assert result["content"] == CONTENT
    assert not set(result) & {
        "events",
        "source_worklog_id",
        "source_hash",
        "instance_id",
        "task_id",
        "reason",
    }
    assert w.call("GET", path + f"/{job['id']}/input", who=reader).status_code == 404
    assert w.call("GET", f"/api/projects/{p}/tasks/{task['id']}", who=reader).status_code == 404
    with w.owner() as db:
        assert (
            db.scalar(text("SELECT count(*) FROM guide_revisions WHERE job_id=:id"), {"id": job["id"]}) == 1
        )


def test_database_blocks_direct_publication_and_missing_review_events(world, database):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from agent_hub.db import context, session_factory

    w = world
    p, _, _, _, job = fixture(w)
    runtime = session_factory(database[0])
    try:
        with runtime() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            with pytest.raises(DBAPIError):
                db.execute(text("UPDATE guide_jobs SET state='published',published_at=now(),version=version+1 "
                                "WHERE id=:id"), {"id": job["id"]})
        draft = generate(w, p, job)
        with runtime() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            db.execute(text("UPDATE guide_jobs SET state='approved',version=version+1 WHERE id=:id"),
                       {"id": draft["id"]})
            with pytest.raises(DBAPIError):
                db.commit()
        assert w.ok("GET", f"/api/projects/{p}/guides/{job['id']}", who=w.root)["state"] == "review"
    finally:
        runtime.kw["bind"].dispose()


def test_project_can_require_explicit_guide_plan_and_older_edits_preserve_the_setting(world):
    from test_collaboration import settings
    from test_tasks import new_task

    w, p = world, world.project()
    settings(w, p, require_guides=True)
    assert settings(w, p, allow_compact=True)["require_guides"] is True
    body = {"title": "说明计划", "description": "功能计划", "acceptance": "完成验收",
            "scope": {"repository": "web", "platform": "web", "paths": ["src"]}}
    assert w.call("POST", f"/api/projects/{p}/tasks", who=w.root, body=body).json()["code"] == "GUIDE_PLAN_REQUIRED"
    task = new_task(w, p, **body, documentation={"kind": "none", "reason": "仅内部重构，不改变任何入口和对外行为。"})
    assert task["documentation"]["reason"]
