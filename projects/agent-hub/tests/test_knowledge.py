# ruff: noqa: F811
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_documents import CONFIG, indexer  # noqa: F401
from test_memory import drain, extractor, note, sync  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, developer, ready
from test_work_policy import prepare

from agent_hub.db import context, session_factory
from agent_hub.memory import encoded
from agent_hub.models import Grant, MemoryFact, MemoryFactEvent

QUOTE = "保持 Web 接口边界，仅修改获准 src 目录。"


def source(w, p, worker, run=None):
    run = run or claim(w, p, ready(w, p, title="Web 接口契约"))
    log = note(w, p, run, notes=QUOTE)
    drain(worker)
    card = next(c for c in w.ok("GET", f"/api/projects/{p}/memory?include_history=true", who=w.root)["items"]
                if c["source"]["id"] == log["id"])
    return run, card


def proposal(w, p, card, who=None, idem=None, **overrides):
    body = {"source_card_id": card["id"], "source_hash": card["source_hash"], "source_quote": QUOTE,
            "topic": "web.interface.scope", "kind": "constraint", "title": "Web 接口边界",
            "body": "开发 Web 接口时仅修改已经获得授权的 src 目录，跨端变更须先调整任务范围。",
            "reason": "本条接口约定需要在后续开发及跨人交接中持续保留。",
            "generation": {"method": "local-agent", "harness": "codex", "model_reported": "fixture-model",
                           "input_tokens_reported": 1200, "output_tokens_reported": 150}, **overrides}
    return w.call("POST", f"/api/projects/{p}/memory/knowledge", who=who or w.root, body=body, idem=idem)


def approve(w, p, fact, who=None, **overrides):
    who = who or w.root
    body = {"expected_version": fact["version"], "state": "active", "reason": "已核对引用原文与当前代码范围，批准这项长期约定。", **overrides}
    if fact["author_id"] == who.user_id and who is w.root:
        body.setdefault("override_reason", "超管本人核对当前来源，显式记录此次自审的限制。")
    return w.call("POST", f"/api/projects/{p}/memory/knowledge/{fact['id']}/decisions", who=who, body=body)


def knowledge(w, p, who=None, suffix=""):
    return w.ok("GET", f"/api/projects/{p}/memory/knowledge{suffix}", who=who or w.root)


def test_proposal_has_verified_quote_is_pending_and_idempotent(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    key = str(uuid.uuid4())
    first = proposal(w, p, card, idem=key)
    assert first.status_code == 200, first.text
    fact = first.json()
    assert fact["state"] == "pending" and fact["version"] == 1
    assert proposal(w, p, card, idem=key).json() == fact
    assert not knowledge(w, p)["items"]
    assert knowledge(w, p, suffix="?include_history=true")["items"][0]["id"] == fact["id"]
    assert fact["generation"]["input_tokens_reported"] == 1200
    assert "author-reported" in fact["generation_usage_trust"]
    assert proposal(w, p, card, source_quote="没有在引用原文中出现过的事实").json()["code"] == "KNOWLEDGE_QUOTE_MISMATCH"
    assert proposal(w, p, card, source_hash="0" * 64).json()["code"] == "KNOWLEDGE_SOURCE_HASH_MISMATCH"


def test_author_cannot_independently_approve_and_root_override_is_explicit(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    fact = proposal(w, p, card).json()
    denied = approve(w, p, fact, override_reason="")
    assert denied.status_code == 403 and denied.json()["code"] == "KNOWLEDGE_INDEPENDENT_REVIEW_REQUIRED"
    approved = approve(w, p, fact)
    assert approved.status_code == 200, approved.text
    assert approved.json()["review_override_reason"] and approved.json()["reviewer_id"] == w.root.user_id
    publisher = w.administrator(p)
    w.start(publisher, w.grant(publisher, p, ["project:read", "tasks:write", "tasks:publish", "logs:read"]))
    own = proposal(w, p, card, who=publisher, topic="web.publisher.scope").json()
    assert approve(w, p, own, who=publisher, override_reason="普通作者不应靠填写自审原因批准自己。").status_code == 403
    assert approve(w, p, own).status_code == 200


def test_long_term_fact_survives_new_handoff_and_is_loaded_within_budget(world, extractor):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    fact = proposal(w, p, card).json()
    assert approve(w, p, fact).status_code == 200
    note(w, p, run, notes="新的 Web 交接仅记录本轮进展，不应抹掉长期接口约定。")
    drain(extractor)
    cards = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"]
    assert card["id"] not in {c["id"] for c in cards}
    assert knowledge(w, p)["items"][0]["id"] == fact["id"]
    bundle = prepare(w, p, run)["payload"]["memory"]
    assert bundle["knowledge"][0]["id"] == fact["id"]
    assert "source_quote" not in bundle["knowledge"][0]
    assert len(encoded(bundle)) <= bundle["byte_budget"]


def test_topic_replacement_requires_old_version_and_keeps_review_history(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    old = approve(w, p, proposal(w, p, card).json()).json()
    new = proposal(w, p, card, body="Web 接口的调整仍须保持已经授权的目录边界，并先核对最新任务要求。").json()
    assert approve(w, p, new).json()["code"] == "KNOWLEDGE_TOPIC_CONFLICT"
    assert approve(w, p, new, replaces_id=old["id"], replaces_version=1).json()["code"] == "KNOWLEDGE_VERSION_CONFLICT"
    response = approve(w, p, new, replaces_id=old["id"], replaces_version=old["version"])
    assert response.status_code == 200, response.text
    items = knowledge(w, p, suffix="?include_history=true")["items"]
    assert {c["state"] for c in items} == {"active", "superseded"}
    assert knowledge(w, p)["items"][0]["id"] == new["id"]
    with w.owner() as db:
        events = list(db.scalars(select(MemoryFactEvent).where(MemoryFactEvent.fact_id == uuid.UUID(old["id"]))))
        assert {e.state for e in events} == {"pending", "active", "superseded"}


def test_concurrent_topic_approval_has_one_winner(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    facts = [proposal(w, p, card, title=f"Web 候选 {n}").json() for n in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda f: approve(w, p, f), facts))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert next(r for r in results if r.status_code == 409).json()["code"] == "KNOWLEDGE_TOPIC_CONFLICT"
    assert len(knowledge(w, p)["items"]) == 1


def test_source_retirement_invalidates_fact_and_loaded_context_synchronously(world, extractor):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    fact = approve(w, p, proposal(w, p, card).json()).json()
    prepared = prepare(w, p, run)
    assert prepared["payload"]["memory"]["knowledge"]
    w.ok("POST", f"/api/projects/{p}/memory/{card['id']}/decisions", who=w.root,
         body={"expected_version": 0, "state": "retired", "reason": "原文结论已有新决定，停止用于后续开发。"})
    assert not knowledge(w, p)["items"]
    stale = knowledge(w, p, suffix="?include_history=true")["items"][0]
    assert stale["state"] == "source_stale" and stale["automatic_state_change"]
    assert stale["version"] == fact["version"] + 1
    status = f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{prepared['id']}/status?run_id={run['run']['id']}&lease_epoch={run['run']['lease_epoch']}"
    assert w.call("GET", status, who=w.root).json()["code"] == "WORK_MEMORY_STALE"
    assert approve(w, p, stale).json()["code"] == "KNOWLEDGE_SOURCE_STALE"


def test_fact_review_change_invalidates_prepared_knowledge(world, extractor):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    fact = approve(w, p, proposal(w, p, card).json()).json()
    prepared = prepare(w, p, run)
    w.ok("POST", f"/api/projects/{p}/memory/knowledge/{fact['id']}/decisions", who=w.root,
         body={"expected_version": fact["version"], "state": "conflicted", "reason": "接口约定发现冲突，等待进一步核对具体适用版本。"})
    status = f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{prepared['id']}/status?run_id={run['run']['id']}&lease_epoch={run['run']['lease_epoch']}"
    assert w.call("GET", status, who=w.root).json()["code"] == "WORK_MEMORY_STALE"


def test_document_unchanged_sync_preserves_fact_changed_source_invalidates_it(world, database, indexer, extractor):
    w, p = world, world.project()
    w.ok("PUT", f"/api/projects/{p}/documents/repository", who=w.root, body=CONFIG)
    files = [("docs/api.md", "# Web 接口\n" + QUOTE)]
    sync(w, p, database[0], indexer, files)
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    fact = approve(w, p, proposal(w, p, card).json()).json()
    sync(w, p, database[0], indexer, files)
    assert knowledge(w, p)["items"][0]["version"] == fact["version"]
    sync(w, p, database[0], indexer, [("docs/api.md", "# Web 接口\n这份契约已经更新，需要重新确认。")])
    assert not knowledge(w, p)["items"]
    assert knowledge(w, p, suffix="?include_history=true")["items"][0]["review_state"] == "source_stale"


def test_configuration_change_hides_facts_and_releases_old_topic(world, database, indexer, extractor):
    w, p = world, world.project()
    base = f"/api/projects/{p}/documents"
    w.ok("PUT", base + "/repository", who=w.root, body=CONFIG)
    sync(w, p, database[0], indexer, [("docs/api.md", QUOTE)])
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    old = approve(w, p, proposal(w, p, card).json()).json()
    w.ok("PUT", base + "/repository", who=w.root, body={**CONFIG, "expected_version": 1})
    assert not knowledge(w, p, suffix="?include_history=true")["items"]
    with w.owner() as db:
        assert db.get(MemoryFact, uuid.UUID(old["id"])).status == "source_stale"
    sync(w, p, database[0], indexer, [("docs/api.md", QUOTE)])
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    assert approve(w, p, proposal(w, p, card).json()).status_code == 200


def test_knowledge_inherits_repository_and_log_permissions(world, extractor, database):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    facts = []
    for repo in (web, pc):
        _, card = source(w, p, extractor, claim(w, p, task(w, p, repo)))
        facts.append(approve(w, p, proposal(w, p, card).json()).json())
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write"])]))
    visible = knowledge(w, p, member)["items"]
    assert [c["source"]["repository_id"] for c in visible] == [web["id"]]
    assert facts[1]["id"] not in json.dumps(knowledge(w, p, member, "?include_history=true"))
    assert knowledge(w, p, member, f"?repository_id={pc['id']}")["items"] == []
    with w.owner.begin() as db:
        row = db.get(Grant, uuid.UUID(member.tokens["grant_id"]))
        row.capabilities = [c for c in row.capabilities if c != "logs:read"]
    assert not knowledge(w, p, member, "?include_history=true")["items"]


def test_cross_project_and_wrong_repository_cannot_propose_or_review(world, extractor):
    w, p = world, world.project()
    repo, other = register(w, p), register(w, p, "pc")
    _, card = source(w, p, extractor, claim(w, p, task(w, p, repo)))
    assert proposal(w, p, card, repository_id=other["id"]).status_code == 404
    other_project = w.project("project-b")
    assert proposal(w, other_project, card).status_code == 404
    fact = proposal(w, p, card).json()
    assert approve(w, other_project, fact).status_code == 404
    assert approve(w, p, fact, repository_id=other["id"]).status_code == 404


def test_runtime_cannot_rewrite_fact_or_commit_unrecorded_state(world, extractor, database):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    proposal(w, p, card)
    runtime = session_factory(database[0])
    for sql in ("UPDATE memory_facts SET body='forged text'", "DELETE FROM memory_facts",
                "DELETE FROM memory_fact_events", "UPDATE memory_facts SET status='active',version=version+1"):
        with pytest.raises(DBAPIError), runtime.begin() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            db.execute(text(sql))
    runtime.kw["bind"].dispose()


def test_proposal_replay_cannot_return_knowledge_after_source_access_is_lost(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    member = developer(w, p, caps=["project:read", "tasks:write", "logs:read"])
    key = str(uuid.uuid4())
    first = proposal(w, p, card, who=member, idem=key)
    assert first.status_code == 200, first.text
    with w.owner.begin() as db:
        grant_row = db.get(Grant, uuid.UUID(member.tokens["grant_id"]))
        grant_row.capabilities = ["project:read", "tasks:write"]
    assert proposal(w, p, card, who=member, idem=key).status_code == 404
    assert not knowledge(w, p, member, "?include_history=true")["items"]


def test_archive_audit_verifies_knowledge_citation_content_and_decisions(world, extractor):
    from agent_hub.memory_maintenance import audit
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    fact = approve(w, p, proposal(w, p, card).json()).json()
    result = audit(w.owner)
    assert result["healthy"] and result["knowledge_facts"] == 1 and result["knowledge_events"] == 2
    with w.owner.begin() as db:
        db.get(MemoryFact, uuid.UUID(fact["id"])).body = "档案被不正确地修改，需要审计发现来源摘要不一致。"
    result = audit(w.owner)
    assert not result["healthy"]
    assert result["errors"][0]["code"] == "KNOWLEDGE_PROVENANCE_MISMATCH"


def test_hidden_log_topic_conflict_does_not_disclose_fact(world, database, indexer, extractor):
    w, p = world, world.project()
    repo = register(w, p)
    _, log_card = source(w, p, extractor, claim(w, p, task(w, p, repo)))
    hidden = approve(w, p, proposal(w, p, log_card).json()).json()
    base = f"/api/projects/{p}/repositories/{repo['id']}/documents"
    w.ok("PUT", base + "/repository", who=w.root, body={"expected_version": 0,
         "repository_url": repo["repository_url"], "branch": repo["branch"], "include_paths": ["docs"]})
    w.ok("POST", base + "/syncs", who=w.root, body={})
    from test_documents import fixture_source
    assert indexer(fixture_source(database[0], [("docs/api.md", QUOTE)])).run_once()
    drain(extractor)
    card = next(c for c in w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"]
                if c["source"]["kind"] == "document")
    pending = proposal(w, p, card).json()
    publisher = w.administrator(p)
    w.start(publisher, grant(w, publisher, p, [(repo, ["code:read", "documents:read"])],
                            caps=["project:read", "tasks:publish", "documents:read"]))
    assert [f["id"] for f in knowledge(w, p, publisher, "?include_history=true")["items"]] == [pending["id"]]
    conflict = approve(w, p, pending, who=publisher)
    assert conflict.status_code == 409 and conflict.json()["code"] == "KNOWLEDGE_TOPIC_CONFLICT"
    assert hidden["id"] not in conflict.text and hidden["author_id"] not in conflict.text
    assert len(knowledge(w, p)["items"]) == 1


def test_review_replay_cannot_export_fact_after_source_access_is_lost(world, extractor):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    fact = proposal(w, p, card).json()
    publisher = w.administrator(p)
    w.start(publisher, w.grant(publisher, p, ["project:read", "tasks:publish", "logs:read"]))
    body = {"expected_version": fact["version"], "state": "active", "reason": "另一身份核对来源后批准，重放仍须验证当前读取权限。"}
    key = str(uuid.uuid4())
    path = f"/api/projects/{p}/memory/knowledge/{fact['id']}/decisions"
    assert w.call("POST", path, who=publisher, body=body, idem=key).status_code == 200
    with w.owner.begin() as db:
        row = db.get(Grant, uuid.UUID(publisher.tokens["grant_id"]))
        row.capabilities = [c for c in row.capabilities if c != "logs:read"]
    assert w.call("POST", path, who=publisher, body=body, idem=key).status_code == 404
