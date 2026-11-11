"""Batch retrieval keeps live source authority, order and revision semantics."""
# ruff: noqa: F811
import uuid

from sqlalchemy import event, select, text
from test_knowledge import approve, proposal, source
from test_knowledge_evidence import citation
from test_memory import extractor  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, lease

from agent_hub import experience, knowledge, memory
from agent_hub.db import context, session_factory
from agent_hub.models import Grant, MemoryCard, MemoryFact, MemoryJob, Task
from agent_hub.schemas import ExperienceRecall, ExperienceVersion, MemorySearch


def test_many_facts_share_bounded_source_queries_and_keep_exact_request_order(world, extractor, database):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    facts = [approve(w, p, proposal(w, p, card, topic=f"web.batch-{i}").json()).json() for i in range(12)]
    factory = session_factory(database[0])
    queries = []
    def counted(*args):
        queries.append(args[2])
    event.listen(factory.kw["bind"], "before_cursor_execute", counted)
    try:
        with factory() as db:
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            current_task = db.get(Task, uuid.UUID(run["task"]["id"]))
            queries.clear()
            found = knowledge.search(db, uuid.UUID(p), MemorySearch(search_mode="keyword", limit=50))
            assert {f["id"] for f in found["items"]} == {f["id"] for f in facts}
            assert len(queries) <= 4  # facts, distinct sources, source events, review events
            assert all(f["state_reason"] == facts[0]["state_reason"] for f in found["items"])
            requested = [uuid.UUID(f["id"]) for f in reversed(facts)]
            body = ExperienceRecall(**lease(run), memory_session_id=uuid.uuid4(), experience_ids=requested,
                                    max_tokens=4096, limit=12)
            queries.clear()
            candidates, _ = experience.NativeEngine().candidates(db, uuid.UUID(p), body, current_task)
            assert [item["id"] for _, item in candidates] == [str(value) for value in requested]
            assert len(queries) <= 3
            known = [ExperienceVersion(kind="knowledge", id=f["id"], revision=f["revision"]) for f in facts]
            queries.clear()
            checked = experience.recall(db, uuid.UUID(p), current_task,
                                        body.model_copy(update={"known": known, "validate_only": True}))
            assert not checked["items"] and not checked["invalidated"]
            assert checked["budget"]["used"] <= body.max_tokens
            assert len(queries) <= 3
    finally:
        factory.kw["bind"].dispose()


def test_reused_session_refreshes_revisions_and_rechecks_secondary_source_revocation(world, extractor, database):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    run, first = source(w, p, extractor, claim(w, p, task(w, p, web)))
    _, second = source(w, p, extractor, claim(w, p, task(w, p, pc)))
    fact = approve(w, p, proposal(w, p, first, additional_sources=[citation(second)]).json()).json()
    reader = w.member(p)
    w.start(reader, grant(w, reader, p, [(web, ["code:read"]), (pc, ["code:read"])],
                          caps=["project:read", "logs:read"]))
    factory = session_factory(database[0])
    try:
        with factory() as db:
            context(db, reader.user_id, reader.enrollment["device_id"], reader.tokens["grant_id"])
            current_task = db.get(Task, uuid.UUID(run["task"]["id"]))
            # Hold the ORM identity strongly: later queries must refresh this row.
            stored = db.get(MemoryFact, uuid.UUID(fact["id"]))
            before = experience.current(db, uuid.UUID(p), "knowledge", stored.id, uuid.UUID(web["id"]))
            assert before["additional_sources"][0]["source"]["repository_id"] == pc["id"]
            # A source state event changes a card revision in the same runtime session.
            w.ok("POST", f"/api/projects/{p}/memory/{first['id']}/decisions", who=w.root,
                 body={"expected_version": 0, "state": "active", "reason": "重新核对当前来源，记录新的状态版本。"})
            changed = experience.current(db, uuid.UUID(p), "card", uuid.UUID(first["id"]), uuid.UUID(web["id"]))
            assert changed["state_version"] == 1 and changed["state_reason"].startswith("重新核对")
            response = approve(w, p, fact, state="retired", override_reason="")
            assert response.status_code == 200, response.text
            retired = response.json()
            current = knowledge.view(db, knowledge.get(db, uuid.UUID(p), stored.id))
            assert current["version"] == retired["version"] and current["state"] == "retired"
            assert stored.version == retired["version"]
            # Restore the fact, then revoke only the secondary source while the DB session stays open.
            renewed = approve(w, p, retired).json()
            before = experience.current(db, uuid.UUID(p), "knowledge", stored.id, uuid.UUID(web["id"]))
            assert before["version"] == renewed["version"]
            with w.owner.begin() as owner:
                row = owner.get(Grant, uuid.UUID(reader.tokens["grant_id"]))
                row.repository_permissions = [v for v in row.repository_permissions if v["repository_id"] != pc["id"]]
            body = ExperienceRecall(**lease(run), memory_session_id=uuid.uuid4(), validate_only=True,
                                    known=[ExperienceVersion(kind="knowledge", id=stored.id, revision=before["revision"])])
            checked = experience.recall(db, uuid.UUID(p), current_task, body)
            assert checked["invalidated"] == ["EXP-" + fact["id"]]
            assert not checked["items"]
            assert experience.current(db, uuid.UUID(p), "knowledge", stored.id, uuid.UUID(web["id"])) is None
            assert not memory.read_cards(db, uuid.UUID(p), [uuid.UUID(second["id"])])
    finally:
        factory.kw["bind"].dispose()


def test_source_set_policies_equal_original_invoker_helper_before_and_after_revocation(world, extractor, database):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    for repo in (web, pc):
        source(w, p, extractor, claim(w, p, task(w, p, repo)))
    reader = w.member(p)
    w.start(reader, grant(w, reader, p, [(web, ["code:read"]), (pc, ["code:read"])],
                          caps=["project:read", "logs:read"]))
    with w.owner() as owner:
        sources = {model: list(owner.execute(select(model.id, model.worklog_id, model.document_id)))
                   for model in (MemoryCard, MemoryJob)}
    factory = session_factory(database[0])
    try:
        with factory() as db:
            context(db, reader.user_id, reader.enrollment["device_id"], reader.tokens["grant_id"])
            for revoking in (False, True):
                if revoking:
                    with w.owner.begin() as owner:
                        row = owner.get(Grant, uuid.UUID(reader.tokens["grant_id"]))
                        row.repository_permissions = [v for v in row.repository_permissions if v["repository_id"] != pc["id"]]
                for model, rows in sources.items():
                    expected = {row.id for row in rows if db.scalar(
                        text("SELECT hub.memory_source_visible(:wid,:did)"), {"wid":row.worklog_id,"did":row.document_id})}
                    assert set(db.scalars(select(model.id))) == expected
                    assert len(expected) == (1 if revoking else 2)
    finally:
        factory.kw["bind"].dispose()
