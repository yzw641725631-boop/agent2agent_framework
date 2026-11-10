# ruff: noqa: F811
import json
import uuid

from test_knowledge import approve, proposal, source
from test_memory import drain, extractor, note  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, lease
from test_work_policy import prepare

from agent_hub.memory_budget import Counter, serialized


def endpoint(p, run, ctx):
    return f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{ctx['id']}/experience"


def args(run, **values):
    return {**lease(run), "memory_session_id": str(uuid.uuid4()), "max_tokens": 4096, **values}


def known(item):
    return {key: item[key] for key in ("id", "kind", "revision")}


def test_full_response_budget_and_exact_id_then_incremental_omission(world, extractor):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    fact = approve(w, p, proposal(w, p, card).json()).json()
    ctx = prepare(w, p, run)
    path = endpoint(p, run, ctx)
    body = args(run, experience_ids=[fact["id"]])
    first = w.ok("POST", path, who=w.root, body=body)
    assert first["items"][0]["experience_id"] == "EXP-" + fact["id"]
    assert first["items"][0]["revision"] == fact["revision"]
    assert first["retrieval"]["semantic_search"] == "not-used-exact-id"
    assert first["budget"]["serialized_bytes"] == len(serialized(first).encode())
    assert first["budget"]["used"] == Counter().count(first) <= body["max_tokens"]
    second = w.ok("POST", path, who=w.root, body={**body, "known": [known(first["items"][0])]})
    assert not second["items"] and second["unchanged"] == 1
    assert fact["body"] not in json.dumps(second, ensure_ascii=False)
    assert second["budget"]["used"] < first["budget"]["used"]
    # A tiny budget must not return an oversized first experience.
    large = approve(
        w,
        p,
        proposal(
            w, p, card, topic="web.large-experience", body="中文经验必须保留来源并核验当前版本。" * 100
        ).json(),
    ).json()
    small = w.ok("POST", path, who=w.root, body={**body, "experience_ids": [large["id"]], "max_tokens": 1024})
    assert small["budget"]["used"] <= 1024
    assert not small["items"] and small["omitted"] == 1


def test_new_experience_does_not_mutate_acknowledged_rules_and_retirement_invalidates_delta(world, extractor):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    ctx = prepare(w, p, run)
    fact = approve(w, p, proposal(w, p, card).json()).json()
    path = endpoint(p, run, ctx)
    body = args(run, experience_ids=[fact["id"]])
    first = w.ok("POST", path, who=w.root, body=body)
    assert first["items"]
    retired = approve(w, p, fact, state="retired", override_reason="")
    assert retired.status_code == 200, retired.text
    checked = w.ok(
        "POST", path, who=w.root, body={**body, "validate_only": True, "known": [known(first["items"][0])]}
    )
    assert checked["invalidated"] == ["EXP-" + fact["id"]]
    assert fact["body"] not in json.dumps(checked, ensure_ascii=False)
    status = w.ok(
        "GET",
        f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{ctx['id']}/status?run_id={run['run']['id']}&lease_epoch={run['run']['lease_epoch']}",
        who=w.root,
    )
    assert status["content_hash"] == ctx["content_hash"]


def test_unacknowledged_wrong_run_and_cross_repository_do_not_export_experience(world, extractor):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    pc_run = claim(w, p, task(w, p, pc))
    note(w, p, pc_run, notes="pc-repository-private-experience")
    drain(extractor)
    private = w.ok("GET", f"/api/projects/{p}/memory?repository_id={pc['id']}", who=w.root)["items"][0]
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write"])]))
    run = claim(w, p, task(w, p, web), member)
    ctx = prepare(w, p, run, member, acknowledge=False)
    path = endpoint(p, run, ctx)
    body = args(run, known=[{"id": private["id"], "kind": "card", "revision": "a" * 64}], validate_only=True)
    assert w.call("POST", path, who=member, body=body).json()["code"] == "WORK_CONTEXT_ACK_REQUIRED"
    ctx = prepare(w, p, run, member)
    checked = w.ok("POST", endpoint(p, run, ctx), who=member, body=body)
    assert checked["invalidated"] == ["SRC-" + private["id"]]
    assert "pc-repository-private" not in json.dumps(checked)
    assert (
        w.call(
            "POST", endpoint(p, run, ctx), who=member, body={**body, "run_id": str(uuid.uuid4())}
        ).status_code
        != 200
    )


def test_read_budget_stops_new_retrieval_and_validation_is_still_available(world, extractor):
    w, p = world, world.project()
    run, _ = source(w, p, extractor)
    ctx = prepare(w, p, run)
    result = w.ok("POST", endpoint(p, run, ctx), who=w.root, body=args(run, consumed_units=24000))
    assert not result["items"] and result["delivery"]["state"] == "read-budget-exhausted"
    assert "logging and handoff" in result["gaps"][0]


def test_reference_tokenizer_counts_entire_json_and_corruption_degrades_without_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HUB_MEMORY_TOKENIZER_FILE", str(tmp_path / "missing"))
    counter = Counter()
    assert counter.method == "utf8-bytes-conservative-fallback"
    value = {"items": ["中文经验"], "metadata": "x" * 1200}
    assert not counter.measure(value, 1024)
    assert value["budget"]["used"] == len(serialized(value).encode())
