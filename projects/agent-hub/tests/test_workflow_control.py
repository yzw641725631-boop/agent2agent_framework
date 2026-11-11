"""Real PostgreSQL enforcement, durable escalation and limited work preservation."""

import json
import uuid
from datetime import timedelta

import pytest
from conftest import PASSWORD
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_tasks import claim, developer, gate, lease, log_body, new_task, ready, submit

from agent_hub.db import context
from agent_hub.memory_worker import MemoryWorker
from agent_hub.models import Idempotency, Task, TaskRun, TaskWorkflowEvent
from agent_hub.security import now


def base(project, task):
    return f"/api/projects/{project}/tasks/{task['id']}"


def overdue(w, task, *, expired_lease=False):
    with w.owner.begin() as db:
        row = db.get(Task, uuid.UUID(task["id"]))
        row.workflow_runtime = {
            **row.workflow_runtime,
            "deadline_at": (now() - timedelta(seconds=1)).isoformat(),
        }
        if expired_lease:
            row.lease_until = now() - timedelta(seconds=1)


def resume(w, project, task, **overrides):
    return w.call(
        "POST",
        base(project, task) + "/workflow-resume",
        who=w.root,
        body={
            "expected_version": task["version"],
            "expected_revision": task["workflow_control"]["revision"],
            "reason": "负责人已核对现有证据并安排下一步处理。",
            "current_password": PASSWORD,
            **overrides,
        },
    )


def test_rejections_are_durable_and_retries_do_not_count_twice(world):
    w = world
    project = w.project()
    task = new_task(w, project)
    url = base(project, task)
    deadline = task["workflow_control"]["deadline_at"]
    for count in range(1, 4):
        key = str(uuid.uuid4())
        body = {
            "expected_version": task["version"],
            "gate": "requirements",
            "approved": False,
            "evidence": "需求范围尚需业务负责人确认。",
            "instance_id": str(uuid.uuid4()),
            "harness": "human",
        }
        task = w.ok("POST", url + "/decisions", who=w.root, body=body, idem=key)
        assert w.ok("POST", url + "/decisions", who=w.root, body=body, idem=key) == task
        assert task["workflow_control"]["rejections"] == {"requirements": count}
        assert task["workflow_control"]["deadline_at"] == deadline
        assert task["workflow_control"]["manual_required"] == (count == 3)
    response = w.call(
        "POST", url + "/decisions", who=w.root, body={**body, "expected_version": task["version"]}
    )
    assert response.status_code == 409 and response.json()["code"] == "TASK_MANUAL_RESOLUTION_REQUIRED"
    details = w.ok("GET", url, who=w.root)
    assert len(details["decisions"]) == 3
    assert len(details["workflow_events"]) == 1
    assert details["workflow_events"][0]["author_id"] is None
    with w.owner() as db:
        assert len(list(db.scalars(select(TaskWorkflowEvent)))) == 1


def test_second_test_return_escalates_and_human_recovery_keeps_history(world):
    w = world
    project = w.project()
    task = ready(w, project)
    for count in (1, 2):
        claimed = claim(w, project, task)
        task, _ = submit(w, project, claimed)
        task = gate(w, project, task, "review", override_reason="超管单人项目明确核对本次变更与证据。")
        task = gate(
            w, project, task, "test", approved=False, override_reason="超管明确记录测试未通过及返工原因。"
        )
        assert task["workflow_control"]["test_returns"] == count
        assert task["workflow_control"]["manual_required"] == (count == 2)
    assert task["workflow_control"]["reason"] == "test_returns"
    assert resume(w, project, task, current_password="incorrect").json()["code"] == "INVALID_CREDENTIALS"
    assert resume(w, project, task, expected_version=1).json()["code"] == "WORKFLOW_RESOLUTION_CONFLICT"
    response = resume(w, project, task)
    assert response.status_code == 200
    newer = response.json()["task"]
    assert newer["workflow_control"]["cycle"] == 2
    assert newer["workflow_control"]["rejections"] == {}
    assert resume(w, project, task).json()["code"] == "WORKFLOW_RESOLUTION_CONFLICT"
    details = w.ok("GET", base(project, task), who=w.root)
    assert [row["kind"] for row in details["workflow_events"]] == ["resumed", "manual_required"]
    assert details["workflow_events"][1]["snapshot"]["test_returns"] == 2
    with w.owner() as db:
        # The password-bearing endpoint never stores an idempotent response or request.
        assert not any("workflow-resume" in row.scope for row in db.scalars(select(Idempotency)))


def test_timeout_preserves_owner_work_but_fences_all_progress_and_old_run_after_recovery(world):
    w = world
    project = w.project()
    task = ready(w, project)
    dev = developer(w, project)
    claimed = claim(w, project, task, dev)
    url = base(project, task)
    deadline = claimed["task"]["workflow_control"]["deadline_at"]
    renewed = w.ok("POST", url + "/heartbeat", who=dev, body=lease(claimed))
    assert renewed["task"]["workflow_control"]["deadline_at"] == deadline
    overdue(w, task, expired_lease=True)
    assert not w.ok("GET", url, who=dev)["coordination"]["claimable"]
    denied = w.call("POST", url + "/heartbeat", who=dev, body=lease(claimed))
    assert denied.json()["code"] == "TASK_MANUAL_RESOLUTION_REQUIRED"
    assert "Do not uninstall" in denied.json()["instructions"]
    paused = w.ok("GET", url, who=w.root)["task"]
    assert paused["workflow_control"]["state"] == "manual"
    assert (
        w.call(
            "POST", url + "/claim", who=dev, body={"instance_id": str(uuid.uuid4()), "harness": "codex"}
        ).json()["code"]
        == "TASK_MANUAL_RESOLUTION_REQUIRED"
    )
    log = w.ok("POST", url + "/worklogs", who=dev, body=log_body(claimed, outcome="blocked"))
    assert log["run_id"] == claimed["run"]["id"]
    other = developer(w, project, "other-developer")
    assert w.call("POST", url + "/worklogs", who=other, body=log_body(claimed)).status_code == 403
    assert w.call(
        "POST",
        url + "/workflow-resume",
        who=dev,
        body={
            "expected_version": paused["version"],
            "expected_revision": paused["workflow_control"]["revision"],
            "reason": "成员试图跳过负责人处理重新启动任务。",
            "current_password": PASSWORD,
        },
    ).status_code in (403, 404)
    newer = resume(w, project, paused).json()["task"]
    assert newer["status"] == "ready" and newer["active_run_id"] is None
    assert (
        w.call("POST", url + "/worklogs", who=dev, body=log_body(claimed)).json()["code"]
        == "TASK_LEASE_STALE"
    )
    assert claim(w, project, newer, dev)["run"]["lease_epoch"] > claimed["run"]["lease_epoch"]
    events = w.ok("GET", f"/api/projects/{project}/progress", who=dev)["items"]
    system = next(e for e in events if e["kind"] == "task.manual_required")
    assert system["source"] == "system" and system["author_id"] is None and system["device_id"] is None
    assert "负责人已核对" not in json.dumps(events, ensure_ascii=False)


def test_watchdog_works_without_a_user_request_and_skips_busy_projects(world):
    from agent_hub.db import coordinate

    w = world
    project = w.project()
    task = new_task(w, project)
    overdue(w, task)
    with w.owner.begin() as busy:
        coordinate(busy, project_ids=[project])
        with w.owner.begin() as db:
            assert db.scalar(text("SELECT hub.expire_task_workflows(100)")) == 0
    worker = MemoryWorker(None, factory=w.owner)
    worker.run_once()
    control = w.ok("GET", base(project, task), who=w.root)["task"]["workflow_control"]
    assert control["state"] == "manual" and control["reason"] == "stage_timeout"
    with w.owner.begin() as db:
        assert db.scalar(text("SELECT hub.expire_task_workflows(100)")) == 0


def test_database_runtime_cannot_reset_control_or_advance_a_paused_task(world):
    w = world
    project = w.project()
    task = new_task(w, project)
    overdue(w, task)
    w.call(
        "POST",
        base(project, task) + "/claim",
        who=w.root,
        body={"instance_id": str(uuid.uuid4()), "harness": "human"},
    )
    for sql in [
        "UPDATE tasks SET workflow_runtime='{}' WHERE id=:id",
        "UPDATE tasks SET status='ready',lease_epoch=lease_epoch+1 WHERE id=:id",
    ]:
        with w.client.app.state.sessions() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            with pytest.raises(DBAPIError):
                db.execute(text(sql), {"id": task["id"]})


def test_queue_does_not_timeout_and_release_reclaim_does_not_reset_development_clock(world):
    w = world
    project = w.project()
    task = ready(w, project)
    assert task["workflow_control"]["deadline_at"] is None
    claimed = claim(w, project, task)
    deadline = claimed["task"]["workflow_control"]["deadline_at"]
    released = w.ok(
        "POST",
        base(project, task) + "/release",
        who=w.root,
        body={**lease(claimed), "reason": "暂时释放，等待同事接续。"},
    )
    assert released["workflow_control"]["deadline_at"] == deadline
    newer = claim(w, project, released)
    assert newer["task"]["workflow_control"]["deadline_at"] == deadline


def test_project_limits_are_versioned_and_do_not_rewrite_existing_tasks(world):
    w = world
    project = w.project()
    old = new_task(w, project)
    config = w.ok(
        "PUT",
        f"/api/projects/{project}/collaboration",
        who=w.root,
        body={
            "expected_version": 0,
            "profiles": [],
            "workflow_limits": {"stage_rejections": 1, "test_returns": 1, "stage_timeout_seconds": 3600},
            "change_reason": "负责人批准当前项目采用更严格的失败处理限制。",
        },
    )
    task = new_task(w, project)
    assert task["workflow_control"]["limits"] == config["workflow_limits"]
    assert task["workflow_control"]["configuration_version"] == config["version"]
    assert (
        w.ok("GET", base(project, old), who=w.root)["task"]["workflow_control"]["limits"]["stage_rejections"]
        == 3
    )
    assert gate(w, project, task, "requirements", approved=False)["workflow_control"]["manual_required"]


def test_revocation_can_end_a_paused_run_without_resuming_it(world):
    from agent_hub import closeouts

    w = world
    project = w.project()
    dev = developer(w, project)
    task = ready(w, project)
    claimed = claim(w, project, task, dev)
    overdue(w, task)
    w.call("POST", base(project, task) + "/heartbeat", who=dev, body=lease(claimed))
    with w.client.app.state.sessions.begin() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        closeouts.create(
            w.client.app.state.hub,
            db,
            user_id=uuid.UUID(dev.user_id),
            project_id=uuid.UUID(project),
            reason="membership_removed",
        )
    newer = w.ok("GET", base(project, task), who=w.root)["task"]
    assert newer["active_run_id"] is None and newer["workflow_control"]["manual_required"]
    with w.owner() as db:
        assert db.get(TaskRun, uuid.UUID(claimed["run"]["id"])).state == "released"


def test_paused_observed_work_can_be_backed_up_but_current_protected_paths_still_apply(world):
    from test_work_archives import compress, setup_archive, upload
    from test_work_policy import publish

    w = world
    project, _, task, run, who, log, value, _ = setup_archive(w, member=True)
    overdue(w, task, expired_lease=True)
    body = log_body(
        run,
        outcome="blocked",
        context_id=log["context_id"],
        change_evidence=value["evidence"],
        git_commit=value["evidence"]["head_commit"],
    )
    saved = w.ok("POST", base(project, task) + "/worklogs", who=who, body=body)
    value["worklog_id"] = saved["id"]
    receipt = upload(w, base(project, task) + f"/worklogs/{saved['id']}/snapshot", compress(value), who)
    assert receipt.status_code == 200 and receipt.json()["working_tree_restorable"]
    # A new protection rule still applies even though the old context can preserve evidence.
    publish(w, project, protected_paths=["src/login.ts"])
    response = w.call("POST", base(project, task) + "/worklogs", who=who, body=body)
    assert response.json()["code"] == "WORK_PATH_PROTECTED"
