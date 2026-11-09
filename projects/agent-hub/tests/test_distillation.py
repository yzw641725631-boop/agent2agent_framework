"""Real PostgreSQL scheduling/leases/ACLs; candidate conclusions supplied by a synthetic local author."""

# ruff: noqa: F811

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_documents import CONFIG, fixture_source, indexer  # noqa: F401
from test_memory import drain, extractor  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, lease, log_body

from agent_hub.db import context
from agent_hub.distillation import DistillationJob
from agent_hub.memory import encoded
from agent_hub.models import MemoryFact, Task
from agent_hub.security import now


def configure(w, p, repo, **values):
    return w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/distillation-policy",
        who=w.root,
        body={
            "expected_version": 0,
            "enabled": True,
            "reason": "持续整理本仓库开发来源，形成有界的提炼待办。",
            **values,
        },
    )


def record(w, p, repo, notes="共享登录边界：新设备完成邮箱核验后才能进入获准的工程。", **values):
    claimed = claim(w, p, task(w, p, repo, **values))
    base = f"/api/projects/{p}/tasks/{claimed['task']['id']}"
    log = w.ok("POST", base + "/worklogs", who=w.root, body=log_body(claimed, notes=notes))
    w.ok(
        "POST", base + "/release", who=w.root, body={**lease(claimed), "reason": "保留工作记录以便继续核对。"}
    )
    return log, claimed


def fixture(w, worker, **policy):
    p, repo = w.project(), None
    repo = register(w, p)
    configure(w, p, repo, **policy)
    record(w, p, repo)
    record(w, p, repo, notes="共享实现记录：验证码过期后重发，原验证码必须作废。")
    drain(worker)
    return p, repo


def jobs(w, p, who=None, **query):
    from urllib.parse import urlencode

    return w.ok("GET", f"/api/projects/{p}/distillations?" + urlencode(query), who=who or w.root)


def acquire(w, p, job, who=None, instance=None, key=None):
    instance = instance or str(uuid.uuid4())
    bundle = w.ok(
        "POST",
        f"/api/projects/{p}/distillations/{job['id']}/claim",
        who=who or w.root,
        body={"expected_version": job["version"], "instance_id": instance},
        idem=key,
    )
    return bundle, instance


def candidate(bundle):
    sources = bundle["sources"]
    return {
        "source_card_id": sources[0]["source_card_id"],
        "source_hash": sources[0]["source_hash"],
        "source_quote": sources[0]["body"][:300],
        "topic": "web.auth.device-approval",
        "kind": "interface",
        "title": "新设备验证与验证码失效边界",
        "body": "实现与回归应继续核对新设备验证和验证码过期的处理边界。",
        "reason": "按本批开发来源整理后续开发需要保留的边界。",
        "additional_sources": [
            {
                "source_card_id": s["source_card_id"],
                "source_hash": s["source_hash"],
                "source_quote": s["body"][:300],
            }
            for s in sources[1:]
        ],
        "generation": {"method": "local-agent", "harness": "codex"},
    }


def completion(bundle, instance, **values):
    job = bundle["job"]
    return {
        "expected_version": job["version"],
        "instance_id": instance,
        "lease_epoch": job["lease_epoch"],
        "source_hash": job["source_hash"],
        "outcome": "proposed",
        "candidates": [candidate(bundle)],
        "reason": "已核对来源与已审核知识，本轮提炼仅作为待审候选。",
        **values,
    }


def test_sources_queue_automatically_without_starting_a_model_and_only_propose_pending_knowledge(
    world, extractor
):
    w = world
    p, repo = fixture(w, extractor)
    page = jobs(w, p)
    assert len(page["items"]) == 1 and page["automatic_model_execution"] is False
    queued = page["items"][0]
    assert queued["state"] == "queued" and not queued["generation_reported"] and queued["source_count"] == 2
    bundle, instance = acquire(w, p, queued)
    assert len(bundle["sources"]) == 2 and bundle["measured_input_bytes"] <= 16000
    assert len(encoded(bundle)) == bundle["measured_input_bytes"]
    assert not bundle["existing_knowledge"] and bundle["review_required"]
    path = f"/api/projects/{p}/distillations/{queued['id']}/complete"
    key, body = str(uuid.uuid4()), completion(bundle, instance)
    result = w.ok("POST", path, who=w.root, body=body, idem=key)
    assert result["job"]["state"] == "succeeded" and result["review_required"]
    assert result["candidates"][0]["state"] == "pending"
    assert len(result["candidates"][0]["additional_sources"]) == 1
    assert result["candidates"][0]["generation"]["prompt_version"] == "distillation-v1"
    assert w.ok("POST", path, who=w.root, body=body, idem=key) == result
    with w.owner() as db:
        assert len(list(db.scalars(select(MemoryFact)))) == 1
    drain(extractor)
    assert len(jobs(w, p)["items"]) == 1
    assert jobs(w, p, repository_id=repo["id"], state="queued")["items"] == []


def test_concurrent_devices_or_instances_can_never_generate_under_the_same_lease(world, extractor):
    w = world
    p, _ = fixture(w, extractor)
    job = jobs(w, p)["items"][0]
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        return w.call(
            "POST",
            f"/api/projects/{p}/distillations/{job['id']}/claim",
            who=w.root,
            body={"expected_version": job["version"], "instance_id": str(uuid.uuid4())},
        )

    with ThreadPoolExecutor(2) as pool:
        responses = [f.result(timeout=15) for f in [pool.submit(attempt), pool.submit(attempt)]]
    assert sorted(r.status_code for r in responses) == [200, 409]
    bundle = next(r.json() for r in responses if r.status_code == 200)
    path = f"/api/projects/{p}/distillations/{job['id']}/complete"
    assert (
        w.call("POST", path, who=w.root, body=completion(bundle, str(uuid.uuid4()))).json()["code"]
        == "DISTILLATION_LEASE_STALE"
    )
    with w.owner.begin() as db:
        # Test owner expires a lease while keeping required transition evidence consistent.
        db.execute(text("ALTER TABLE distillation_jobs DISABLE TRIGGER distillation_transition"))
        db.get(DistillationJob, uuid.UUID(job["id"])).lease_until = now() - timedelta(seconds=1)
        db.flush()
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        db.execute(text("ALTER TABLE distillation_jobs ENABLE TRIGGER distillation_transition"))
    updated = jobs(w, p)["items"][0]
    replacement, new_instance = acquire(w, p, updated)
    assert replacement["job"]["lease_epoch"] > bundle["job"]["lease_epoch"]
    assert (
        w.call("POST", path, who=w.root, body=completion(bundle, bundle["job"]["instance_id"])).status_code
        == 409
    )
    result = w.ok(
        "POST",
        path,
        who=w.root,
        body=completion(replacement, new_instance, outcome="no_new_knowledge", candidates=[]),
    )
    assert result["job"]["outcome"] == "no_new_knowledge" and result["candidates"] == []


def test_all_source_permissions_are_required_on_listing_read_claim_and_cached_replay(world, extractor):
    w = world
    p, repo = fixture(w, extractor)
    member = w.member(p)
    w.start(member, grant(w, member, p, [(repo, ["code:read"])], caps=["project:read", "tasks:write"]))
    assert jobs(w, p, member)["items"] == []
    root_job = jobs(w, p)["items"][0]
    base = f"/api/projects/{p}/distillations/{root_job['id']}"
    assert w.call("GET", base + "/input", who=member).status_code == 404
    assert (
        w.call(
            "POST",
            base + "/claim",
            who=member,
            body={"expected_version": 1, "instance_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    w.start(
        member,
        grant(w, member, p, [(repo, ["code:read"])], caps=["project:read", "tasks:write", "logs:read"]),
    )
    key, instance = str(uuid.uuid4()), str(uuid.uuid4())
    bundle, _ = acquire(w, p, root_job, member, instance, key)
    alien_repo = register(w, p, "pc")
    assert w.call("GET", base + "/input?repository_id=" + alien_repo["id"], who=w.root).status_code == 404
    w.start(member, grant(w, member, p, [(repo, ["code:read"])], caps=["project:read", "tasks:write"]))
    assert jobs(w, p, member)["items"] == []
    assert (
        w.call(
            "POST",
            base + "/claim",
            who=member,
            body={"expected_version": 1, "instance_id": instance},
            idem=key,
        ).status_code
        == 404
    )
    assert (
        w.call("POST", base + "/complete", who=member, body=completion(bundle, instance)).status_code == 404
    )


def test_policy_changes_cancel_old_leases_and_allow_fresh_batches_without_changing_originals(
    world, extractor
):
    w = world
    p, repo = fixture(w, extractor)
    old = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, old)
    configure(w, p, repo, expected_version=1)
    page = jobs(w, p)
    assert len(page["items"]) == 2
    assert next(j for j in page["items"] if j["id"] == old["id"])["state"] == "obsolete"
    response = w.call(
        "POST",
        f"/api/projects/{p}/distillations/{old['id']}/complete",
        who=w.root,
        body=completion(bundle, instance),
    )
    assert response.json()["code"] == "DISTILLATION_SOURCE_CHANGED"
    queued = next(j for j in page["items"] if j["state"] == "queued")
    new_bundle, _ = acquire(w, p, queued)
    assert new_bundle["sources"] == bundle["sources"]


def test_new_handoff_retains_immutable_old_worklogs_for_distillation_and_keeps_their_dates(world, extractor):
    w = world
    p, repo = fixture(w, extractor)
    job = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, job)
    with w.owner() as db:
        source_task = db.scalar(select(Task).where(Task.project_id == uuid.UUID(p)))
        task_data = w.ok("GET", f"/api/projects/{p}/tasks/{source_task.id}", who=w.root)["task"]
    run = claim(w, p, task_data)
    w.ok(
        "POST",
        f"/api/projects/{p}/tasks/{task_data['id']}/worklogs",
        who=w.root,
        body=log_body(run, notes="更新后的接口行为与先前记录不同，旧来源需要重新核对。"),
    )
    response = w.ok(
        "POST",
        f"/api/projects/{p}/distillations/{job['id']}/complete",
        who=w.root,
        body=completion(bundle, instance),
    )
    assert response["job"]["state"] == "succeeded"
    assert response["candidates"][0]["state"] == "pending"
    assert all(s["source_created_at"] for s in bundle["sources"])


def test_retiring_a_source_rejects_its_claimed_batch_before_worker_scheduling(world, extractor):
    w = world
    p, _ = fixture(w, extractor)
    job = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, job)
    source_id = bundle["sources"][0]["source_card_id"]
    w.ok(
        "POST",
        f"/api/projects/{p}/memory/{source_id}/decisions",
        who=w.root,
        body={"expected_version": 0, "state": "retired", "reason": "这项来源已退役，不再用于提炼长期结论。"},
    )
    response = w.call(
        "POST",
        f"/api/projects/{p}/distillations/{job['id']}/complete",
        who=w.root,
        body=completion(bundle, instance),
    )
    assert response.json()["code"] == "DISTILLATION_SOURCE_CHANGED"


def test_document_replacement_rejects_old_lease_and_requeues_current_sources(
    world, database, indexer, extractor
):
    w, p = world, world.project()
    repo = register(w, p, "docs")
    configure(w, p, repo, min_sources=1)
    base = f"/api/projects/{p}/repositories/{repo['id']}/documents"
    w.ok("PUT", base + "/repository", who=w.root, body={k: v for k, v in CONFIG.items() if k != "token"})

    def sync(content):
        w.ok("POST", base + "/syncs", who=w.root, body={})
        assert indexer(fixture_source(database[0], [("docs/auth.md", content)])).run_once()

    sync("# 登录边界\n设备必须完成验证后才允许进入项目。")
    drain(extractor)
    job = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, job)
    assert bundle["sources"][0]["source_path"] == "docs/auth.md"
    sync("# 登录边界\n登录规则已更新，成员要按最新流程完成设备核验。")
    response = w.call(
        "POST",
        f"/api/projects/{p}/distillations/{job['id']}/complete",
        who=w.root,
        body=completion(bundle, instance),
    )
    assert response.json()["code"] == "DISTILLATION_SOURCE_CHANGED"
    drain(extractor)
    page = jobs(w, p)["items"]
    assert next(j for j in page if j["id"] == job["id"])["state"] == "obsolete"
    new = next(j for j in page if j["state"] == "queued")
    fresh, _ = acquire(w, p, new)
    assert fresh["sources"][0]["source_hash"] != bundle["sources"][0]["source_hash"]


def test_candidates_cannot_cite_outside_sources_and_retries_preserve_auditable_reason(world, extractor):
    w = world
    p, repo = fixture(w, extractor)
    job = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, job)
    record(w, p, repo, notes="另一个后续工作来源，不属于冻结的本批来源。")
    drain(extractor)
    body = completion(bundle, instance)
    body["candidates"][0]["source_card_id"] = str(uuid.uuid4())
    base = f"/api/projects/{p}/distillations/{job['id']}"
    assert (
        w.call("POST", base + "/complete", who=w.root, body=body).json()["code"]
        == "DISTILLATION_CITATION_OUTSIDE_BATCH"
    )
    deferred = w.ok(
        "POST",
        base + "/retry",
        who=w.root,
        body={
            k: v
            for k, v in completion(bundle, instance).items()
            if k in {"expected_version", "instance_id", "lease_epoch", "reason"}
        },
    )
    assert deferred["state"] == "queued" and deferred["reason"]
    new_bundle, _ = acquire(w, p, deferred)
    assert new_bundle["job"]["attempts"] == 2


def test_disabled_configuration_never_queues_or_executes_and_database_requires_events(world, extractor):
    w = world
    p, repo = fixture(w, extractor, enabled=False)
    assert jobs(w, p)["items"] == []
    configure(w, p, repo, expected_version=1)
    job = jobs(w, p)["items"][0]
    with pytest.raises(DBAPIError), w.client.app.state.sessions.begin() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        db.execute(text("UPDATE distillation_jobs SET source_refs='[]' WHERE id=:id"), {"id": job["id"]})


def test_existing_knowledge_is_available_for_deduplication_with_hard_input_budget(world, extractor):
    from test_knowledge import approve

    w = world
    p, _ = fixture(w, extractor, input_budget_bytes=10000)
    bundle, _ = acquire(w, p, jobs(w, p)["items"][0])
    for number in range(4):
        body = {**candidate(bundle), "topic": f"web.auth.existing-{number}", "body": "有效约定" * 450}
        fact = w.ok("POST", f"/api/projects/{p}/memory/knowledge", who=w.root, body=body)
        assert approve(w, p, fact).status_code == 200
    job = jobs(w, p)["items"][0]
    result = w.ok("GET", f"/api/projects/{p}/distillations/{job['id']}/input", who=w.root)
    assert result["existing_knowledge"] and result["existing_knowledge_truncated"]
    assert len(encoded(result)) == result["measured_input_bytes"] <= result["input_budget_bytes"] == 10000
    assert all(source["content_trust"] == "untrusted-project-data" for source in result["sources"])
    assert (
        w.call(
            "PUT",
            f"/api/projects/{p}/repositories/{job['repository_id']}/distillation-policy",
            who=w.root,
            body={"expected_version": 1, "enabled": True, "reason": " " * 10},
        ).status_code
        == 422
    )


def test_project_budget_blocks_new_generation_but_keeps_pending_results_and_retry_available(world, extractor):
    from test_insights import report

    w = world
    p, repo = fixture(w, extractor)
    job = jobs(w, p)["items"][0]
    bundle, instance = acquire(w, p, job)
    _, run = record(w, p, repo)
    record(w, p, repo)
    drain(extractor)
    other = next(j for j in jobs(w, p)["items"] if j["state"] == "queued")
    retry_bundle, retry_instance = acquire(w, p, other)
    assert report(w, p, run).status_code == 200
    w.ok(
        "PUT",
        f"/api/projects/{p}/usage/budget",
        who=w.root,
        body={
            "expected_version": 0,
            "mode": "pause-managed-work",
            "monthly_reported_token_limit": 120,
            "change_reason": "达到已上报额度后暂停新的提炼，保留既有结果的保存能力。",
        },
    )
    completed = w.ok(
        "POST",
        f"/api/projects/{p}/distillations/{job['id']}/complete",
        who=w.root,
        body=completion(bundle, instance, outcome="no_new_knowledge", candidates=[]),
    )
    assert completed["job"]["state"] == "succeeded"
    deferred = w.ok(
        "POST",
        f"/api/projects/{p}/distillations/{other['id']}/retry",
        who=w.root,
        body={
            "expected_version": retry_bundle["job"]["version"],
            "instance_id": retry_instance,
            "lease_epoch": retry_bundle["job"]["lease_epoch"],
            "reason": "额度已经暂停，本实例交回本批资料等待下次处理。",
        },
    )
    response = w.call(
        "POST",
        f"/api/projects/{p}/distillations/{other['id']}/claim",
        who=w.root,
        body={"expected_version": deferred["version"], "instance_id": instance},
    )
    assert response.json()["code"] == "USAGE_BUDGET_EXCEEDED"
