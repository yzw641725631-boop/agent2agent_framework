# ruff: noqa: F811
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from test_documents import CONFIG, fixture_source, indexer  # noqa: F401
from test_knowledge import approve, proposal, source
from test_memory import drain, extractor, note  # noqa: F401
from test_repositories import register
from test_tasks import claim, developer, ready
from test_work_policy import prepare

from agent_hub.db import context, session_factory
from agent_hub.embedding_worker import EmbeddingWorker
from agent_hub.memory import encoded
from agent_hub.models import EmbeddingJob
from agent_hub.security import now
from agent_hub.semantic import DIMENSIONS, MODEL, Embeddings, vector_literal


@pytest.fixture
def semantic(world, database):
    cfg, owner = database
    encoder = Embeddings(cfg.model_copy(update={"embedding_url": "http://embedding:8766"}))
    world.client.app.state.embeddings = encoder
    engine = create_engine(owner.kw["bind"].url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def role(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE hub_embedding")
        connection.commit()

    worker = EmbeddingWorker(cfg, factory=sessionmaker(engine, expire_on_commit=False), embeddings=encoder)
    yield worker
    engine.dispose()


def cards(w, p, query, who=None, **args):
    from urllib.parse import urlencode

    return w.ok("GET", f"/api/projects/{p}/memory?" + urlencode({"query": query, **args}), who=who or w.root)


def test_real_cpu_embeddings_find_synonyms_and_preserve_identifier_keywords(world, extractor, semantic):
    w, p = world, world.project()
    notes = [
        "成员退出后应先保存尚未提交的修改，再移除设备上的受管文件。",
        "服务器数据库每天定时备份，并验证能够恢复原始记录。",
        "支付接口通过 transaction_id 阻止重复扣款。",
    ]
    ids = []
    for index, value in enumerate(notes):
        run = claim(w, p, ready(w, p, title=f"独立模块 {index}"))
        ids.append(note(w, p, run, notes=value)["id"])
    drain(extractor)
    drain(semantic)
    result = cards(w, p, "离职交接清理本机框架")
    assert result["semantic_search"] == "ready" and "rrf" in result["search_mode"]
    assert result["items"][0]["source"]["id"] == ids[0]
    assert "semantic" in result["items"][0]["retrieved_by"]
    exact = cards(w, p, "transaction_id", search_mode="keyword")
    assert len(exact["items"]) == 1 and exact["items"][0]["source"]["id"] == ids[2]
    assert exact["semantic_search"] == "keyword-requested"
    status = w.ok("GET", f"/api/projects/{p}/memory/status", who=w.root)
    assert status["embeddings"]["cards"] == {"visible": 3, "indexed": 3, "pending_or_failed": 0}
    assert status["embeddings"]["dimensions"] == 512


def test_reviewed_knowledge_is_indexed_only_after_approval_and_loaded_in_budget(world, extractor, semantic):
    w, p = world, world.project()
    run, card = source(w, p, extractor)
    fact = proposal(w, p, card).json()
    drain(semantic)
    with w.owner() as db:
        assert not db.scalar(select(EmbeddingJob).where(EmbeddingJob.fact_id == uuid.UUID(fact["id"])))
    assert approve(w, p, fact).status_code == 200
    drain(semantic)
    result = w.ok("GET", f"/api/projects/{p}/memory/knowledge?query=网页开发允许修改什么文件", who=w.root)
    assert result["semantic_search"] == "ready" and result["items"][0]["id"] == fact["id"]
    bundle = prepare(w, p, run)["payload"]["memory"]
    assert bundle["retrieval"]["semantic_search"] == "ready"
    assert len(encoded(bundle)) <= bundle["byte_budget"] == 12000
    assert bundle["knowledge"][0]["id"] == fact["id"]


def test_query_failure_falls_back_without_blocking_logs_and_explicit_keyword_skips_model(
    world, extractor, semantic
):
    w, p = world, world.project()
    run = claim(w, p, ready(w, p))
    note(w, p, run, notes="邮件交接说明")
    drain(extractor)
    world.client.app.state.embeddings.url = "http://127.0.0.1:1"
    result = cards(w, p, "邮件")
    assert result["items"] and result["semantic_search"] == "unavailable-keyword-fallback"
    assert cards(w, p, "邮件", search_mode="keyword")["semantic_search"] == "keyword-requested"
    assert note(w, p, run, notes="模型故障期间仍保存原文")["id"]
    bundle = prepare(w, p, run)["payload"]["memory"]
    assert "embedding-unavailable: this preparation used keyword retrieval" in bundle["gaps"]


def test_query_reauthenticates_after_network_and_rejects_concurrent_removal(world, extractor, semantic):
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)), notes="同事共享敏感交接")
    drain(extractor)
    drain(semantic)
    member = developer(w, p, caps=["project:read", "logs:read"])
    encoder = world.client.app.state.embeddings
    original = encoder.query

    def revoke(query):
        # Would time out if the API still held project/identity locks over I/O.
        w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})
        return original(query)

    encoder.query = revoke
    response = w.call("GET", f"/api/projects/{p}/memory?query=敏感", who=member)
    assert response.status_code in {401, 403}
    assert "同事共享敏感交接" not in response.text


def test_semantic_candidates_and_queue_counts_cannot_cross_original_permissions(
    world, extractor, semantic, database
):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    fact = proposal(w, p, card).json()
    assert approve(w, p, fact).status_code == 200
    drain(semantic)
    member = developer(w, p)
    assert cards(w, p, "Web", who=member)["items"] == []
    state = w.ok("GET", f"/api/projects/{p}/memory/status", who=member)
    assert state["embeddings"]["cards"]["visible"] == 0
    assert state["embeddings"]["knowledge"]["visible"] == 0
    assert state["embeddings"]["visible_jobs"] == {}
    runtime = session_factory(database[0])
    with runtime.begin() as db:
        context(db, member.user_id, member.enrollment["device_id"], member.tokens["grant_id"])
        for table in ["memory_embeddings", "knowledge_embeddings", "embedding_jobs"]:
            assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    runtime.kw["bind"].dispose()


def test_repository_bound_semantic_search_and_source_invalidation(
    world, extractor, indexer, semantic, database
):
    w, p = world, world.project()
    first, second = register(w, p, "web"), register(w, p, "pc")
    for repo, word in [(first, "网页"), (second, "桌面")]:
        base = f"/api/projects/{p}/repositories/{repo['id']}/documents"
        config = {**CONFIG, "repository_url": repo["repository_url"], "include_paths": ["docs"]}
        config.pop("token")
        w.ok("PUT", base + "/repository", who=w.root, body=config)
        w.ok("POST", base + "/syncs", who=w.root, body={})
        assert indexer(
            fixture_source(
                database[0], [("docs/flow.md", f"# {word}交接\n用户登录之前必须完成设备邮箱验证。")]
            )
        ).run_once()
    drain(extractor)
    drain(semantic)
    for repo in [first, second]:
        result = cards(w, p, "换机器校验身份", repository_id=repo["id"])
        assert result["items"] and all(item["source"]["source_id"] == repo["id"] for item in result["items"])
    base = f"/api/projects/{p}/repositories/{first['id']}/documents"
    config = {
        **CONFIG,
        "repository_url": first["repository_url"],
        "expected_version": 1,
        "include_paths": ["other"],
    }
    config.pop("token")
    w.ok("PUT", base + "/repository", who=w.root, body=config)
    assert cards(w, p, "换机器校验身份", repository_id=first["id"], include_history="true")["items"] == []


def test_worker_retries_and_recovers_expired_lease_without_duplicate_vectors(world, extractor, semantic):
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)))
    drain(extractor)
    encoder, original = semantic.embeddings, semantic.embeddings.encode
    encoder.encode = lambda *_: (_ for _ in ()).throw(ValueError("offline"))
    for attempt in range(3):
        assert semantic.run_once()
        with w.owner.begin() as db:
            job = db.scalar(select(EmbeddingJob))
            assert job.attempts == attempt + 1
            job.not_before = now() - timedelta(seconds=1)
    with w.owner() as db:
        assert db.scalar(select(EmbeddingJob)).status == "failed"
    assert w.ok("POST", f"/api/projects/{p}/memory/retry", who=w.root, body={})["embedding_queued"] == 1
    with w.owner.begin() as db:
        job = db.scalar(select(EmbeddingJob))
        job.status, job.lease_token, job.lease_until = "running", uuid.uuid4(), now() - timedelta(seconds=1)
    encoder.encode = original
    assert semantic.run_once() and not semantic.run_once()
    with w.owner() as db:
        assert db.scalar(text("SELECT count(*) FROM memory_embeddings WHERE model=:m"), {"m": MODEL}) == 1
        assert db.scalar(select(EmbeddingJob)).status == "succeeded"


def test_worker_discards_fact_retired_during_inference_and_cannot_read_raw_logs(world, extractor, semantic):
    w, p = world, world.project()
    _, card = source(w, p, extractor)
    drain(semantic)
    fact = proposal(w, p, card).json()
    fact = approve(w, p, fact).json()
    original = semantic.embeddings.encode

    def retire(value, kind):
        w.ok(
            "POST",
            f"/api/projects/{p}/memory/knowledge/{fact['id']}/decisions",
            who=w.root,
            body={
                "expected_version": fact["version"],
                "state": "retired",
                "reason": "推理期间正式退役，禁止作为当前知识继续索引。",
            },
        )
        return original(value, kind)

    semantic.embeddings.encode = retire
    assert semantic.run_once()
    with w.owner() as db:
        assert (
            db.scalar(select(EmbeddingJob).where(EmbeddingJob.fact_id == uuid.UUID(fact["id"]))).status
            == "skipped"
        )
        assert db.scalar(text("SELECT count(*) FROM knowledge_embeddings")) == 0
    with semantic.sessions.begin() as db, pytest.raises(DBAPIError):
        db.execute(text("SELECT notes FROM worklogs"))


def test_model_dimension_finite_values_and_content_hash_are_enforced(world, extractor, semantic):
    for vector in ([1, 0], [0] * DIMENSIONS, [float("nan")] * DIMENSIONS, [True] * DIMENSIONS):
        with pytest.raises(ValueError):
            vector_literal(vector)
    w, p = world, world.project()
    note(w, p, claim(w, p, ready(w, p)), notes="唯一原文线索")
    drain(extractor)
    drain(semantic)
    with w.owner.begin() as db:
        db.execute(
            text("UPDATE memory_embeddings SET content_hash=repeat('f',64) WHERE model=:m"), {"m": MODEL}
        )
    result = cards(w, p, "完全不同措辞")
    assert result["items"] == []
    status = w.ok("GET", f"/api/projects/{p}/memory/status", who=w.root)
    assert status["embeddings"]["cards"]["pending_or_failed"] == 1


@pytest.mark.parametrize("failure", ["model", "dimensions", "nan", "zero", "oversize", "redirect", "offline"])
def test_model_protocol_failures_explicitly_degrade_to_keywords(database, failure):
    cfg = database[0].model_copy(update={"embedding_url": "http://embedding:8766"})
    data = {"model": MODEL, "dimensions": DIMENSIONS, "vector": [1.0] + [0.0] * (DIMENSIONS - 1)}
    if failure == "model":
        data["model"] = "other-model"
    if failure == "dimensions":
        data["dimensions"] = 384
    if failure == "zero":
        data["vector"] = [0.0] * DIMENSIONS

    def response(request):
        if failure == "nan":
            return httpx.Response(
                200, content=b'{"model":"' + MODEL.encode() + b'","dimensions":512,"vector":[NaN]}'
            )
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 30001)
        if failure == "redirect":
            return httpx.Response(
                302, headers={"location": "https://example.org/never-forward-project-content"}
            )
        if failure == "offline":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json=data)

    encoder = Embeddings(cfg, transport=httpx.MockTransport(response))
    assert encoder.query("交接记忆").state == "unavailable-keyword-fallback"
