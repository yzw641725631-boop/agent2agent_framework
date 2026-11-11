"""24 synthetic questions against native SQL/RLS/pgvector and pinned real CPU embeddings.

All authoring uses the public API; repeated noise is indexed from identical real
embeddings by an isolated fixture to keep inference cost independent of corpus size.
"""
# ruff: noqa: F811
import json
import os
import resource
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from test_documents import CONFIG, fixture_source, indexer  # noqa: F401
from test_knowledge import approve, proposal
from test_memory import drain, extractor, note  # noqa: F401
from test_repositories import grant, register, task
from test_tasks import claim, lease

from agent_hub import experience, knowledge, memory, semantic
from agent_hub.db import context, session_factory
from agent_hub.memory_benchmark import evaluate
from agent_hub.memory_budget import Counter
from agent_hub.models import MemoryCard, MemoryFact, Task, Worklog, ProjectDocument
from agent_hub.schemas import ExperienceRecall, ExperienceVersion, MemorySearch


def source_card(w, log):
    with w.owner() as db:
        row = db.execute(memory.base_query(uuid.UUID(log["project_id"])).where(
            MemoryCard.worklog_id == uuid.UUID(log["id"]))).first()
        return memory.card_view(db, row)


def authored_fact(w, p, card, number, value, **overrides):
    fields = dict(topic=f"benchmark.rule-{number}", title=value[:180], source_quote=value,
                  body=value + "。后续交接继续核验本条来源。", generation={"method": "human"})
    fields.update(overrides)
    candidate = proposal(w, p, card, **fields)
    assert candidate.status_code == 200, candidate.text
    return approve(w, p, candidate.json()).json()


def sync(w, p, repo, files, cfg, indexer):
    base = f"/api/projects/{p}/repositories/{repo['id']}/documents"
    w.ok("POST", base + "/syncs", who=w.root, body={})
    assert indexer(fixture_source(cfg, files)).run_once()


def seed_embeddings(w, encoder, cache):
    started, requests = time.perf_counter(), 0
    with w.owner.begin() as db:
        for model, table, key, kind in [(MemoryCard, "memory_embeddings", "card_id", "card"),
                                         (MemoryFact, "knowledge_embeddings", "fact_id", "fact")]:
            existing = set(db.scalars(text(f"SELECT {key} FROM {table} WHERE model=:m"), {"m": semantic.MODEL}))
            for row in db.scalars(select(model)):
                if row.id in existing or kind == "fact" and row.status != "active":
                    continue
                value = semantic.content(row.title, row.body, row.topic if kind == "fact" else "")
                if value not in cache:
                    cache[value] = semantic.vector_literal(encoder.encode(value, "passage"))
                    requests += 1
                db.execute(text(f"INSERT INTO {table}({key},model,dimensions,content_hash,embedding) "
                    "VALUES(:id,:model,:dim,:hash,CAST(:vector AS vector))"),
                    {"id":row.id,"model":semantic.MODEL,"dim":semantic.DIMENSIONS,"hash":semantic.digest(value),"vector":cache[value]})
    return {"unique_passage_requests":requests,"elapsed_ms":round((time.perf_counter()-started)*1000,3)}


def texts(payload):
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        return [part for item in payload for part in texts(item)]
    if not isinstance(payload, dict):
        return []
    result = [payload[k] for k in ("title","body","summary","next_steps","notes","content") if isinstance(payload.get(k), str)]
    for key in ("items","knowledge","recent_unindexed","startup","recall","logs","documents"):
        if key in payload:
            result.extend(texts(payload[key]))
    return result


def test_representative_questions_at_growing_history_sizes(world, database, extractor, indexer, tmp_path):
    if os.name == "nt":
        pytest.fail("Run this database/model benchmark on server 47 only")
    w, cfg = world, database[0]
    cases = json.loads((Path(__file__).parents[1]/"examples/memory-quality-cases.json").read_text())["cases"]
    counter = Counter()
    p = w.project()
    web, pc, revoked = register(w,p), register(w,p,"pc"), register(w,p,"archived-source")
    run = claim(w,p,task(w,p,web,title="记忆交接与代码备份"))
    facts, distillation = [], []
    old_texts = [case["expected"][0] for case in cases[:6]] + [case["forbidden"][0] for case in cases[6:12]]
    for index, value in enumerate(old_texts):
        log = note(w,p,run,summary=value,notes=value)
        drain(extractor)
        card = source_card(w,log)
        fact = authored_fact(w,p,card,index,value)
        facts.append(fact)
        distillation.append({"source_reference_tokens":counter.count(card),"candidate_reference_tokens":counter.count(fact)})
        if index in (10,11):
            state = "conflicted" if index == 10 else "retired"
            approve(w,p,fact,state=state)
            w.ok("POST",f"/api/projects/{p}/memory/{card['id']}/decisions",who=w.root,
                body={"expected_version":0,"state":state,"reason":"合成基准明确撤回这条来源，不应继续供给模型。"})
    files = [("docs/rules.md", "\n".join("# "+case["id"]+"\n"+case["expected"][0]
        for case in cases[6:10]+cases[16:20]) + "\n# large-fact\n超长经验必须附带来源")]
    for repo in (web,pc,revoked):
        base=f"/api/projects/{p}/repositories/{repo['id']}/documents"
        config={**CONFIG,"repository_url":repo["repository_url"],"include_paths":["docs"]};config.pop("token")
        w.ok("PUT",base+"/repository",who=w.root,body=config)
        private = "桌面仓库专属维护路径" if repo is pc else "已撤销来源专属维护说明"
        sync(w,p,repo,files if repo is web else [("docs/private.md","# Private\n"+private)],cfg,indexer)
    drain(extractor)
    with w.owner() as db:
        cards=[memory.card_view(db,row) for row in db.execute(memory.base_query(uuid.UUID(p),uuid.UUID(web["id"])))
               if row[2] is not None]
    for index, case in enumerate(cases[6:10]+cases[16:20]):
        value=case["expected"][0];card=next(c for c in cards if value in c["body"])
        candidate=proposal(w,p,card,topic=f"benchmark.rule-{index+6}" if index<4 else f"benchmark.paraphrase-{index}",
            title=value,source_quote=value,body=value+"。交接时核验当前来源。",generation={"method":"human"}).json()
        extra={"replaces_id":facts[index+6]["id"],"replaces_version":facts[index+6]["version"]} if index<4 else {}
        response=approve(w,p,candidate,**extra);assert response.status_code==200,response.text
    large=next(c for c in cards if "超长经验必须附带来源" in c["body"])
    authored_fact(w,p,large,30,"超长经验必须附带来源",body="超长经验必须附带来源。"+"请逐条核验当前来源和版本。"*80)
    reader=w.member(p,login="benchmark-reader")
    w.start(reader,grant(w,reader,p,[(web,["code:read","documents:read"]),(revoked,["code:read","documents:read"])],caps=["project:read","logs:read","documents:read"]))
    w.start(reader,grant(w,reader,p,[(web,["code:read","documents:read"])],caps=["project:read","logs:read","documents:read"]))
    # Every strategy receives the same explicit ID; never reveal an expected ID only to layered retrieval.
    for case in cases:
        if "exact_fact" in case:
            case["query"] = "EXP-" + facts[case["exact_fact"]]["id"]
    encoder=semantic.Embeddings(cfg.model_copy(update={"embedding_url":"http://embedding:8766"}))
    runtime=session_factory(cfg)
    counts=[0]
    def counted(*args): counts[0]+=1
    event.listen(runtime.kw["bind"],"before_cursor_execute",counted)
    cache, reports, previous = {}, [], 0
    try:
        for noise in (64,512,2048):
            files.append((f"docs/history-{noise}.md",("# Archive housekeeping\nRepeated housekeeping records; check the maintenance ledger.\n")*(noise-previous)))
            previous=noise
            sync(w,p,web,files,cfg,indexer);drain(extractor)
            indexing=seed_embeddings(w,encoder,cache)
            # Deliberately do not drain extraction for the newest handoff at this scale.
            note(w,p,run,summary="当前交接：先核验新版备份回执",notes=f"synthetic growth scale {noise}",next_steps="下一步：核验来源撤权后的记忆失效")
            with w.owner() as db:
                corpus_cards=db.scalar(select(text("count(*)")).select_from(MemoryCard))
            def retrieve(strategy,case):
                before=counts[0];requests=0
                with runtime() as db:
                    context(db,reader.user_id,reader.enrollment["device_id"],reader.tokens["grant_id"])
                    current_task=db.get(Task,uuid.UUID(run["task"]["id"]))
                    db.info["semantic_configured"]=True
                    query=case["query"]
                    if strategy in ("hybrid","layered") and not case.get("exact_fact"):
                        vector=encoder.query(query);assert vector.state=="ready"
                        db.info["semantic_query"]=vector;requests=1
                    if strategy=="full":
                        logs=list(db.scalars(select(Worklog).join(Task,Task.id==Worklog.task_id).where(
                            Worklog.project_id==uuid.UUID(p),Task.repository_id==uuid.UUID(web["id"]))))
                        documents=list(db.scalars(select(ProjectDocument).where(ProjectDocument.source_id==uuid.UUID(web["id"]))))
                        payload={"logs":[{"id":str(log.id),"summary":log.summary,"notes":log.notes,"next_steps":log.next_steps} for log in logs],
                                 "documents":[{"path":doc.path,"content":doc.content} for doc in documents]}
                    elif strategy in ("keyword","hybrid"):
                        search=MemorySearch(query=query,repository_id=web["id"],search_mode="keyword" if strategy=="keyword" else "hybrid",limit=6)
                        payload={"items":memory.search(db,uuid.UUID(p),search)["items"],"knowledge":knowledge.search(db,uuid.UUID(p),search,compact=True)["items"]}
                    else:
                        body=ExperienceRecall(**lease(run),memory_session_id=uuid.uuid4(),query=query,max_tokens=case.get("max_tokens",4096),
                            consumed_units=case.get("consumed_units",0),limit=8,
                            experience_ids=[facts[case["exact_fact"]]["id"]] if "exact_fact" in case else [])
                        recalled=experience.recall(db,uuid.UUID(p),current_task,body,counter=counter)
                        startup=memory.prepare_bundle(db,uuid.UUID(p),current_task)
                        payload={"startup":startup,"recall":recalled}
                        known=[{k:item[k] for k in ("id","kind","revision")} for item in recalled["items"]]
                        repeat_started=time.perf_counter()
                        first_queries=counts[0]-before
                        repeated=experience.recall(db,uuid.UUID(p),current_task,body.model_copy(update={"known":[
                            ExperienceVersion(**item) for item in known]}),counter=counter)
                        repeat_ms=(time.perf_counter()-repeat_started)*1000
                        assert recalled["budget"]["used"]<=body.max_tokens and repeated["budget"]["used"]<=body.max_tokens
                        return {"payload":payload,"context_texts":texts(payload),"repeat_payload":repeated,
                            "metrics":{"sql_queries":first_queries,"repeat_sql_queries":counts[0]-before-first_queries,
                                       "repeat_elapsed_ms":round(repeat_ms,3),"embedding_requests":requests,"retries":0,"recall_budget_respected":True}}
                    return {"payload":payload,"context_texts":texts(payload),"metrics":{"sql_queries":counts[0]-before,"embedding_requests":requests,"retries":0}}
            report=evaluate(cases,retrieve,counter=counter)
            report.update(noise_cards=noise,corpus_cards=corpus_cards,indexing=indexing)
            assert all(item["unauthorized_exposures"]==0 for item in report["cases"])
            assert report["summary"]["layered"]["stale_or_conflicted_exposures"]==0
            delta=next(item for item in report["cases"] if item["case"]=="repeat-delta" and item["strategy"]=="layered")
            assert delta["repeat_unchanged"]>0 and delta["repeat_response_reference_tokens"]<delta["response_reference_tokens"]
            reports.append(report)
    finally:
        runtime.kw["bind"].dispose()
    result={"schema_version":1,"source_class":"synthetic","production_data_accessed":False,
        "ranking":"executed-native-SQL-RLS-pgvector-RRF-and-layered-memory",
        "embedding_model":semantic.MODEL,"embedding_dimensions":semantic.DIMENSIONS,
        "index_seed":"isolated-fixture-real-embeddings-cached-by-exact-source-text",
        "query_latency_scope":"initial-service-functions-SQL-and-query-embedding; repeat-validated-separately; excludes-network-and-model-answers",
        "distillation":{"method":"human-fixture-no-model-call","actual_provider_tokens":None,
            "reference_input_tokens":sum(row["source_reference_tokens"] for row in distillation),
            "reference_output_tokens":sum(row["candidate_reference_tokens"] for row in distillation)},
        "provider_usage":None,"provider_billing":None,"answer_correctness":"not-measured-no-completion-model",
        "hindsight":"not-executed","peak_process_rss_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,"runs":reports}
    destination=Path(os.getenv("HUB_MEMORY_BENCHMARK_REPORT",str(tmp_path/"memory-quality-report.json")))
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
