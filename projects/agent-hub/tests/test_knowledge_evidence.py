# ruff: noqa: F811
import uuid

from test_knowledge import QUOTE, approve, knowledge, proposal, source
from test_memory import extractor  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim
from test_work_policy import prepare

from agent_hub.models import Grant, MemoryFact


def citation(card):
    return {"source_card_id": card["id"], "source_hash": card["source_hash"], "source_quote": QUOTE}


def test_multi_source_knowledge_inherits_every_repository_and_replay_reauthorizes(world, extractor):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    _, web_card = source(w, p, extractor, claim(w, p, task(w, p, web)))
    _, pc_card = source(w, p, extractor, claim(w, p, task(w, p, pc)))
    result = proposal(w, p, web_card, additional_sources=[citation(pc_card)])
    assert result.status_code == 200, result.text
    fact = approve(w, p, result.json()).json()
    assert fact["additional_sources"][0]["source"]["repository_id"] == pc["id"]
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read"])]))
    assert not knowledge(w, p, member, "?include_history=true")["items"]
    w.start(member, grant(w, member, p, [(web, ["code:read"]), (pc, ["code:read"])]))
    assert knowledge(w, p, member)["items"][0]["id"] == fact["id"]
    operation = str(uuid.uuid4())
    pending = proposal(w, p, web_card, who=member, idem=operation, additional_sources=[citation(pc_card)])
    assert pending.status_code == 200, pending.text
    with w.owner.begin() as db:
        row = db.get(Grant, uuid.UUID(member.tokens["grant_id"]))
        row.repository_permissions = [
            entry for entry in row.repository_permissions if entry["repository_id"] != pc["id"]
        ]
    assert not knowledge(w, p, member, "?include_history=true")["items"]
    assert proposal(w, p, web_card, who=member, idem=operation, additional_sources=[citation(pc_card)]).status_code == 404
    assert w.call("GET", f"/api/projects/{p}/memory/knowledge/{fact['id']}", who=member).status_code == 404


def test_secondary_conflict_retires_active_fact_invalidates_preparation_and_preserves_originals(
    world, extractor
):
    w, p = world, world.project()
    run, first = source(w, p, extractor)
    _, second = source(w, p, extractor, run)
    fact = approve(w, p, proposal(w, p, first, additional_sources=[citation(second)]).json()).json()
    prepared = prepare(w, p, run)
    status_path = (
        f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{prepared['id']}/status"
        f"?run_id={run['run']['id']}&lease_epoch={run['run']['lease_epoch']}"
    )
    w.ok(
        "POST",
        f"/api/projects/{p}/memory/{second['id']}/decisions",
        who=w.root,
        body={
            "expected_version": second["state_version"],
            "state": "conflicted",
            "reason": "此条工作记录的约定与当前接口发生冲突，需要重新审核。",
        },
    )
    assert not knowledge(w, p)["items"]
    assert w.call("GET", status_path, who=w.root).json()["code"] == "WORK_MEMORY_STALE"
    with w.owner() as db:
        stored = db.get(MemoryFact, uuid.UUID(fact["id"]))
        assert stored.status == "source_stale" and stored.version == fact["version"] + 1
        assert stored.additional_sources == [citation(second)]
    originals = w.ok("GET", f"/api/projects/{p}/memory?include_history=true", who=w.root)["items"]
    assert next(card for card in originals if card["id"] == second["id"])["source_hash"] == second["source_hash"]
    from agent_hub.memory_maintenance import audit

    assert audit(w.owner)["healthy"]


def test_additional_sources_require_exact_quotes_unique_ids_current_state_and_same_project(world, extractor):
    w, p = world, world.project()
    run, first = source(w, p, extractor)
    _, second = source(w, p, extractor, run)
    assert proposal(w, p, first, additional_sources=[citation(first)]).status_code == 422
    assert (
        proposal(w, p, first, additional_sources=[{**citation(second), "source_hash": "0" * 64}]).json()[
            "code"
        ]
        == "KNOWLEDGE_SOURCE_HASH_MISMATCH"
    )
    assert (
        proposal(
            w, p, first, additional_sources=[{**citation(second), "source_quote": "这不是这份原文所说的内容"}]
        ).json()["code"]
        == "KNOWLEDGE_QUOTE_MISMATCH"
    )
    other = w.project("other-project")
    _, foreign = source(w, other, extractor)
    assert proposal(w, p, first, additional_sources=[citation(foreign)]).status_code == 404
