# ruff: noqa: F811
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from test_documents import CONFIG, fixture_source, indexer  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, developer, lease, log_body, ready
from test_work_policy import prepare

from agent_hub.db import context, session_factory
from agent_hub.memory import encoded
from agent_hub.memory_worker import MemoryWorker
from agent_hub.models import DocumentVersion, Grant, MemoryCard, MemoryJob, WorkContext


@pytest.fixture
def extractor(database):
    cfg, owner = database
    engine = create_engine(owner.kw["bind"].url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def role(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE hub_memory")
        connection.commit()

    factory = sessionmaker(engine, expire_on_commit=False)
    yield MemoryWorker(cfg, factory=factory)
    engine.dispose()


def drain(worker):
    for _ in range(200):
        if not worker.run_once():
            return
    raise AssertionError("memory queue did not drain")


def note(w, p, run, **values):
    return w.ok("POST", f"/api/projects/{p}/tasks/{run['task']['id']}/worklogs",
                who=w.root, body=log_body(run, **values))


def sync(w, p, cfg, indexer, files=None):
    base = f"/api/projects/{p}/documents"
    w.ok("POST", base + "/syncs", who=w.root, body={})
    assert indexer(fixture_source(cfg, files)).run_once()


def test_atomic_incremental_queue_and_latest_handoff_with_originals(world, extractor):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p))
    old = note(w, p, run, notes="旧交接原文", next_steps="旧步骤")
    new = note(w, p, run, notes="新交接原文", next_steps="接下来检查 Web 的邮件边界")
    status = w.ok("GET", f"/api/projects/{p}/memory/status", who=w.root)
    assert status["visible_jobs"]["queued"] == 2
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: drain(extractor), range(2)))
    assert not extractor.run_once()
    result = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)
    assert len(result["items"]) == 1
    assert result["items"][0]["source"]["id"] == new["id"]
    assert "接下来检查" in result["items"][0]["body"]
    history = w.ok("GET", f"/api/projects/{p}/memory?include_history=true", who=w.root)
    assert {c["state"] for c in history["items"]} == {"active", "superseded"}
    assert {c["source"]["id"] for c in history["items"]} == {old["id"], new["id"]}
    with w.owner() as db:
        assert len(list(db.scalars(select(MemoryCard)))) == 2
    assert len(w.ok("GET", f"/api/projects/{p}/tasks/{run['task']['id']}/worklogs", who=w.root)["items"]) == 2


def test_worker_rollback_retries_without_publishing_partial_cards(world, extractor, monkeypatch):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p))
    note(w, p, run)
    import agent_hub.memory_worker as module

    def fail(_):
        raise ValueError("not a provider response")

    original = module.extract_log
    monkeypatch.setattr(module, "extract_log", fail)
    for _ in range(3):
        assert extractor.run_once()
    assert not extractor.run_once()
    status = w.ok("GET", f"/api/projects/{p}/memory/status", who=w.root)
    assert status["visible_cards"] == {} and status["visible_jobs"] == {"failed": 1}
    bundle = prepare(w, p, run)["payload"]["memory"]
    assert "extraction-failed" in " ".join(bundle["gaps"])
    assert bundle["recent_unindexed"]
    monkeypatch.setattr(module, "extract_log", original)
    assert w.ok("POST", f"/api/projects/{p}/memory/retry", who=w.root, body={})["queued"] == 1
    drain(extractor)
    assert w.ok("GET", f"/api/projects/{p}/memory/status", who=w.root)["visible_jobs"] == {"succeeded": 1}


def test_source_rls_is_inherited_by_cards_queue_and_originals(world, extractor, database):
    w, p = world, world.project()
    root_run = claim(w, p, ready(w, p))
    note(w, p, root_run, notes="同事私密开发历史")
    member = developer(w, p)
    own_run = claim(w, p, ready(w, p), member)
    own = w.ok("POST", f"/api/projects/{p}/tasks/{own_run['task']['id']}/worklogs", who=member,
               body=log_body(own_run))
    drain(extractor)
    root_cards = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"]
    root_card = next(c for c in root_cards if c["source"]["id"] != own["id"])
    visible = w.ok("GET", f"/api/projects/{p}/memory", who=member)
    assert [c["source"]["id"] for c in visible["items"]] == [own["id"]]
    assert w.call("GET", f"/api/projects/{p}/memory/{root_card['id']}/original", who=member).status_code == 404
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert len(list(db.scalars(select(MemoryCard)))) == 1
        assert len(list(db.scalars(select(MemoryJob)))) == 1
    runtime.kw["bind"].dispose()


def test_multi_repository_cards_do_not_expand_the_grant(world, extractor):
    w, p = world, world.project()
    web, pc = register(w, p), register(w, p, "pc")
    for repo in (web, pc):
        run = claim(w, p, task(w, p, repo))
        note(w, p, run, notes=repo["slug"] + "-secret-details")
    member = w.member(p)
    w.start(member, grant(w, member, p, [(web, ["code:read", "code:write"])]))
    drain(extractor)
    result = w.ok("GET", f"/api/projects/{p}/memory", who=member)
    assert len(result["items"]) == 1 and result["items"][0]["source"]["repository_id"] == web["id"]
    assert "pc-secret" not in json.dumps(result)
    assert w.ok("GET", f"/api/projects/{p}/memory?repository_id={pc['id']}", who=member)["items"] == []


def test_document_versions_deduplicate_and_preserve_exact_bytes(world, database, indexer, extractor):
    w, p = world, world.project()
    w.ok("PUT", f"/api/projects/{p}/documents/repository", who=w.root, body=CONFIG)
    original = b"\xef\xbb\xbf# Architecture\r\nKeep Web scope.\r\n"
    sync(w, p, database[0], indexer, [("docs/a.md", original)])
    sync(w, p, database[0], indexer, [("docs/a.md", original)])
    with w.owner() as db:
        versions = list(db.scalars(select(DocumentVersion)))
        assert len(versions) == 1 and versions[0].original_bytes == original
        assert versions[0].exact_bytes and versions[0].sha256 == hashlib.sha256(original).hexdigest()
    drain(extractor)
    old = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    sync(w, p, database[0], indexer, [("docs/a.md", "# Architecture\nThe new documented contract.")])
    drain(extractor)
    result = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)
    assert "new documented" in json.dumps(result) and "Keep Web" not in json.dumps(result)
    history = w.ok("GET", f"/api/projects/{p}/memory?include_history=true", who=w.root)
    assert len(history["items"]) == 2
    archived = w.ok("GET", f"/api/projects/{p}/memory/{old['id']}/original", who=w.root)
    assert "Keep Web" in archived["content"]


def test_configuration_change_hides_archived_derived_data_synchronously(world, database, indexer, extractor):
    w, p = world, world.project()
    base = f"/api/projects/{p}/documents"
    w.ok("PUT", base + "/repository", who=w.root, body=CONFIG)
    sync(w, p, database[0], indexer)
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    w.ok("PUT", base + "/repository", who=w.root, body={**CONFIG, "expected_version": 1, "include_paths": ["other"]})
    # Worker has not run since the change; old data must already be inaccessible.
    assert w.ok("GET", f"/api/projects/{p}/memory?include_history=true", who=w.root)["items"] == []
    assert w.call("GET", f"/api/projects/{p}/memory/{card['id']}/original", who=w.root).status_code == 404
    with w.owner() as db:
        assert list(db.scalars(select(DocumentVersion)))


def test_opening_bundle_has_byte_budget_origins_and_pending_fallback(world, extractor):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p))
    latest = note(w, p, run, notes="交接说明" * 1000)
    before = prepare(w, p, run)["payload"]["memory"]
    assert before["recent_unindexed"][0]["id"] == latest["id"]
    assert before["coverage"]["visible_jobs"]["queued"] == 1
    drain(extractor)
    after = prepare(w, p, run)["payload"]["memory"]
    assert after["items"][0]["source"]["id"] == latest["id"]
    assert not after["recent_unindexed"]
    assert after["items"][0]["truncated"]
    assert len(encoded(after)) <= after["byte_budget"]
    assert after["serialized_bytes"] >= len(encoded(after))


def test_conflicted_card_is_excluded_and_forces_context_refresh(world, extractor):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p))
    note(w, p, run)
    drain(extractor)
    prepared = prepare(w, p, run)
    card = prepared["payload"]["memory"]["items"][0]
    path = f"/api/projects/{p}/memory/{card['id']}/decisions"
    body = {"expected_version": 0, "state": "conflicted", "reason": "接口结论与现有验收证据冲突，需重新核对。"}
    w.ok("POST", path, who=w.root, body=body)
    assert w.call("POST", path, who=w.root, body=body).json()["code"] == "MEMORY_VERSION_CONFLICT"
    status = f"/api/projects/{p}/tasks/{run['task']['id']}/contexts/{prepared['id']}/status?run_id={run['run']['id']}&lease_epoch={run['run']['lease_epoch']}"
    assert w.call("GET", status, who=w.root).json()["code"] == "WORK_MEMORY_STALE"
    assert w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"] == []
    # Neither the raw original nor the previous derived card was deleted.
    assert w.ok("GET", f"/api/projects/{p}/memory/{card['id']}/original", who=w.root)["content"]


def test_context_receipt_cannot_leak_after_logs_permission_removed(world, extractor, database):
    w, p = world, world.project()
    root_run = claim(w, p, ready(w, p, title="Web 历史接口"))
    note(w, p, root_run, notes="同事共享内容")
    drain(extractor)
    member = developer(w, p, caps=["project:read", "tasks:claim", "tasks:write", "logs:read"])
    run = claim(w, p, ready(w, p), member)
    key = str(uuid.uuid4())
    path = f"/api/projects/{p}/tasks/{run['task']['id']}/contexts"
    prepared = w.ok("POST", path, who=member, body=lease(run), idem=key)
    assert prepared["payload"]["memory"]["items"]
    with w.owner.begin() as db:
        grant_row = db.get(Grant, uuid.UUID(member.tokens["grant_id"]))
        grant_row.capabilities = ["project:read", "tasks:claim", "tasks:write"]
    assert w.call("POST", path, who=member, body=lease(run), idem=key).status_code == 409
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert not list(db.scalars(select(WorkContext)))
    runtime.kw["bind"].dispose()


def test_runtime_cannot_rewrite_or_delete_archive_and_cards(world, database, extractor):
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)))
    drain(extractor)
    runtime = session_factory(database[0])
    for sql in ("DELETE FROM memory_cards", "UPDATE memory_cards SET body='forged'", "DELETE FROM document_versions"):
        with runtime.begin() as db, pytest.raises(DBAPIError):
            context(db, w.root.user_id, w.root.enrollment["device_id"])
            db.execute(text(sql))
    runtime.kw["bind"].dispose()


def test_vector_extension_and_semantic_index_obey_source_rls(world, database, extractor):
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)))
    drain(extractor)
    member = developer(w, p)
    with w.owner.begin() as db:
        card = db.scalar(select(MemoryCard))
        assert db.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'")) == "0.8.7"
        db.execute(text("INSERT INTO memory_embeddings VALUES(:id,'fixture-only',3,:hash,'[1,0,0]')"),
                   {"id": card.id, "hash": card.source_hash})
        assert db.scalar(text("SELECT embedding <=> '[1,0,0]'::vector FROM memory_embeddings")) == 0
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        assert db.scalar(text("SELECT count(*) FROM memory_embeddings")) == 0
    runtime.kw["bind"].dispose()


def test_long_original_line_can_be_read_in_bounded_pages(world, database, indexer, extractor):
    w, p = world, world.project()
    w.ok("PUT", f"/api/projects/{p}/documents/repository", who=w.root, body=CONFIG)
    content = "交接原文" * 5000
    sync(w, p, database[0], indexer, [("docs/long.md", content)])
    drain(extractor)
    card = w.ok("GET", f"/api/projects/{p}/memory", who=w.root)["items"][0]
    assert card["truncated"]
    parts, line, char = [], 1, 0
    for _ in range(10):
        page = w.ok("GET", f"/api/projects/{p}/memory/{card['id']}/original?start_line={line}&start_char={char}", who=w.root)
        assert len(page["content"].encode()) <= 16000
        parts.append(page["content"])
        if not page["has_more"]:
            break
        line, char = page["next_start_line"], page["next_start_char"]
    assert "".join(parts) == content


def test_memory_archive_integrity_audit_detects_altered_source_hash(world, database, extractor):
    from agent_hub.memory_maintenance import audit
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)))
    drain(extractor)
    before = audit(database[1])
    assert before["healthy"] and before["cards"] == 1
    with w.owner.begin() as db:
        db.execute(text("UPDATE memory_cards SET source_hash=:h"), {"h": "f" * 64})
    after = audit(database[1])
    assert not after["healthy"] and after["errors"][0]["code"] == "SOURCE_HASH_MISMATCH"
