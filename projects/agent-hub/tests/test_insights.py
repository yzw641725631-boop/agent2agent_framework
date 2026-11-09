# ruff: noqa: F811
import uuid

from test_memory import drain, extractor, note  # noqa: F401
from test_repositories import grant, register, task
from test_semantic import semantic  # noqa: F401
from test_tasks import claim, developer, ready
from test_work_policy import observed_body, prepare

from agent_hub.memory import encoded
from agent_hub.models import Grant


def report(w, p, run, who=None, **extra):
    return w.call(
        "POST",
        f"/api/projects/{p}/usage",
        who=who or w.root,
        body={
            "task_id": run["task"]["id"],
            "run_id": run["run"]["id"],
            "reference_hash": "c" * 64,
            "provider": "provider-from-host",
            "model": "actual-host-reported-model",
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_input_tokens": 40,
            **extra,
        },
    )


def test_measured_context_report_deduplication_and_budget_preserves_handoff(world):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p, execution_mode="observed"))
    prepared = prepare(w, p, run)
    assert prepared["context_measurement"]["bytes"] == len(encoded(prepared["payload"]))
    assert report(w, p, run).status_code == 200
    assert report(w, p, run).status_code == 200  # New operation ID, same provider reference.
    assert report(w, p, run, input_tokens=200).json()["code"] == "USAGE_RECEIPT_CONFLICT"
    usage = w.ok("GET", f"/api/projects/{p}/usage", who=w.root)
    assert usage["reported_input_tokens"] == 100 and usage["reported_output_tokens"] == 20
    assert usage["reported_cached_input_tokens"] == 40 and usage["reported_cost_micro_usd"] is None
    assert usage["measured_context_bytes"] > 0 and usage["billing_verified"] is False
    w.ok(
        "PUT",
        f"/api/projects/{p}/usage/budget",
        who=w.root,
        body={
            "expected_version": 0,
            "mode": "pause-managed-work",
            "monthly_reported_token_limit": 120,
            "change_reason": "此项目明确采用已上报用量作为暂停受管开发的依据",
        },
    )
    body = {"instance_id": str(uuid.uuid4()), "harness": "codex"}
    other = ready(w, p)
    assert (
        w.call("POST", f"/api/projects/{p}/tasks/{other['id']}/claim", who=w.root, body=body).json()["code"]
        == "USAGE_BUDGET_EXCEEDED"
    )
    base = f"/api/projects/{p}/tasks/{run['task']['id']}"
    status = w.ok(
        "GET", base + f"/contexts/{prepared['id']}/status?run_id={run['run']['id']}&lease_epoch=1", who=w.root
    )
    assert status["usage_budget_open"] is False and status["can_continue"] is True
    w.ok("POST", base + "/worklogs", who=w.root, body=observed_body(run, prepared))


def test_usage_is_owned_by_actual_run_device_and_visible_repository(world):
    w, p = world, world.project()
    member = developer(w, p)
    run = claim(w, p, ready(w, p))
    assert report(w, p, run, member).status_code == 404
    assert report(w, p, run).status_code == 200
    assert w.ok("GET", f"/api/projects/{p}/usage", who=member)["reported_input_tokens"] == 0
    assert (
        w.call(
            "PUT",
            f"/api/projects/{p}/usage/budget",
            who=member,
            body={"expected_version": 0, "change_reason": "普通开发者不能修改全项目的用量配置"},
        ).status_code
        == 404
    )
    assert report(w, p, run, cached_input_tokens=101).status_code == 422


def test_retrieval_evaluation_executes_queries_and_records_real_hits_and_misses(world, extractor):
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)), notes="支付通过 transaction_id 避免重复扣款。")
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    response = w.ok(
        "POST",
        f"/api/projects/{p}/memory/evaluations",
        who=w.root,
        body={
            "k": 1,
            "search_mode": "keyword",
            "cases": [
                {"query": "transaction_id", "expected_card_ids": [card["id"]]},
                {"query": "unfindable_zzzz_reference", "expected_card_ids": [card["id"]]},
            ],
        },
    )
    assert response["payload"]["mean_recall_at_k"] == 0.5
    assert response["payload"]["mean_reciprocal_rank"] == 0.5
    assert all(case["semantic_state"] == "keyword-requested" for case in response["payload"]["cases"])
    assert "unfindable_zzzz_reference" not in str(response)
    assert (
        w.ok("GET", f"/api/projects/{p}/memory/evaluations", who=w.root)["items"][0]["id"] == response["id"]
    )


def test_evaluation_uses_real_local_embeddings(world, extractor, semantic):
    w, p = world, world.project()
    first = note(
        w, p, claim(w, p, ready(w, p)), notes="成员退出后应先保存尚未提交的修改，再移除设备上的受管文件。"
    )
    note(w, p, claim(w, p, ready(w, p)), notes="支付接口通过 transaction_id 阻止重复扣款。")
    drain(extractor)
    drain(semantic)
    card = next(
        item
        for item in w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"]
        if item["source"]["id"] == first["id"]
    )
    result = w.ok(
        "POST",
        f"/api/projects/{p}/memory/evaluations",
        who=w.root,
        body={
            "k": 1,
            "search_mode": "hybrid",
            "cases": [{"query": "离职交接清理本机框架", "expected_card_ids": [card["id"]]}],
        },
    )
    assert result["payload"]["mean_recall_at_k"] == 1
    assert result["payload"]["cases"][0]["semantic_state"] == "ready"
    assert "rrf" in result["payload"]["cases"][0]["actual_search_mode"]


def test_evaluation_cached_results_reauthorize_all_returned_sources(world, extractor):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    first = note(w, p, claim(w, p, task(w, p, web)), notes="shared_evaluation_marker Web 范围")
    note(w, p, claim(w, p, task(w, p, pc)), notes="shared_evaluation_marker PC 范围")
    drain(extractor)
    cards = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"]
    web_card = next(c for c in cards if c["source"]["id"] == first["id"])
    member = w.administrator(p)
    w.start(
        member,
        grant(
            w,
            member,
            p,
            [(web, ["code:read"]), (pc, ["code:read"])],
            caps=["project:read", "tasks:publish", "logs:read"],
        ),
    )
    path, operation = f"/api/projects/{p}/memory/evaluations", str(uuid.uuid4())
    body = {
        "search_mode": "keyword",
        "k": 5,
        "cases": [{"query": "shared_evaluation_marker", "expected_card_ids": [web_card["id"]]}],
    }
    original = w.ok("POST", path, who=member, body=body, idem=operation)
    assert len(original["payload"]["source_card_ids"]) == 2
    with w.owner.begin() as db:
        row = db.get(Grant, uuid.UUID(member.tokens["grant_id"]))
        row.repository_permissions = [
            entry for entry in row.repository_permissions if entry["repository_id"] != pc["id"]
        ]
    assert w.call("POST", path, who=member, body=body, idem=operation).status_code == 404
    assert not w.ok("GET", path, who=member)["items"]
