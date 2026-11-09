"""Measured context size, explicitly reported model usage and real retrieval evaluation."""

import hashlib
import uuid
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy import func, select

from . import memory, tasks
from . import schemas as S
from .models import MemoryEvaluation, TaskRun, UsageEvent, UsagePolicy
from .security import HubError, now, rate_limit
from .work_policy import digest


def budget(db, project_id):
    row = db.scalar(
        select(UsagePolicy)
        .where(UsagePolicy.project_id == project_id)
        .order_by(UsagePolicy.version.desc())
        .limit(1)
    )
    value = {
        "version": row.version if row else 0,
        **(row.content if row else {"mode": "observe", "monthly_reported_token_limit": 0}),
    }
    return {
        **value,
        "content_hash": digest(value),
        "managed_work_available": bool(db.scalar(select(func.hub.usage_budget_open(project_id)))),
        "coverage": "received-host-reports-only; not complete harness billing or an external-process limit",
    }


def require_budget(db, project_id):
    if not db.scalar(select(func.hub.usage_budget_open(project_id))):
        raise HubError("USAGE_BUDGET_EXCEEDED", 409, handoff_and_logging_allowed=True)


def configure(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    previous = budget(db, project_id)
    if body.expected_version != previous["version"]:
        raise HubError("USAGE_POLICY_VERSION_CONFLICT", 409, current_version=previous["version"])
    content = body.model_dump(exclude={"expected_version", "change_reason"})
    row = UsagePolicy(
        project_id=project_id,
        version=previous["version"] + 1,
        author_id=actor.user.id,
        device_id=actor.device.id,
        content=content,
        content_hash=digest(content),
    )
    db.add(row)
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "usage.policy_published",
        row.version,
        request_id,
        project_id,
        reason=body.change_reason,
        content_hash=row.content_hash,
    )
    return budget(db, project_id)


def report_run(db, actor, project_id, body):
    task = tasks.get_task(db, project_id, body.task_id)
    if body.repository_id and body.repository_id != task.repository_id:
        raise HubError("TASK_REPOSITORY_MISMATCH", 422)
    run = db.get(TaskRun, body.run_id)
    if not run or (run.project_id, run.task_id, run.user_id, run.device_id) != (
        project_id,
        task.id,
        actor.user.id,
        actor.device.id,
    ):
        raise HubError("USAGE_RUN_NOT_ACCESSIBLE", 404)
    return task, run


def record_context(db, actor, task, run, context):
    size = len(memory.encoded(context.payload))
    db.add(
        UsageEvent(
            project_id=task.project_id,
            repository_id=task.repository_id,
            task_id=task.id,
            run_id=run.id,
            author_id=actor.user.id,
            device_id=actor.device.id,
            reference_hash=digest(str(context.id)),
            source="context-measurement",
            provider="agent-hub",
            model="no-model-call",
            context_bytes=size,
        )
    )
    return {
        "bytes": size,
        "token_estimate": (size + 2) // 3,
        "estimate_method": "UTF-8 bytes divided by 3, rounded up; not tokenizer or provider usage",
    }


def report(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:write")
    task, run = report_run(db, actor, project_id, body)
    previous = db.scalar(
        select(UsageEvent).where(
            UsageEvent.project_id == project_id,
            UsageEvent.run_id == run.id,
            UsageEvent.source == "host-report",
            UsageEvent.reference_hash == body.reference_hash,
        )
    )
    values = body.model_dump(exclude={"task_id", "run_id", "repository_id"})
    if previous:
        if any(getattr(previous, key) != value for key, value in values.items()):
            raise HubError("USAGE_RECEIPT_CONFLICT", 409)
        row = previous
    else:
        row = UsageEvent(
            project_id=project_id,
            repository_id=task.repository_id,
            task_id=task.id,
            run_id=run.id,
            author_id=actor.user.id,
            device_id=actor.device.id,
            source="host-report",
            **values,
        )
        db.add(row)
        db.flush()
        hub.audit(
            db,
            actor.user.id,
            "usage.reported",
            row.id,
            request_id,
            project_id,
            repository_id=task.repository_id,
            task_id=str(task.id),
            source=row.source,
        )
    return {
        "id": str(row.id),
        "source": row.source,
        "billing_verified": False,
        "reported_tokens": row.input_tokens + row.output_tokens,
        "budget": budget(db, project_id),
    }


def summary(db, project_id, repository_id=None):
    month = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    statement = select(
        func.count(UsageEvent.id),
        func.coalesce(func.sum(UsageEvent.input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.reported_cost_micro_usd), 0),
        func.count(UsageEvent.reported_cost_micro_usd),
        func.coalesce(func.sum(UsageEvent.context_bytes), 0),
    ).where(UsageEvent.project_id == project_id, UsageEvent.created_at >= month)
    if repository_id:
        statement = statement.where(UsageEvent.repository_id == repository_id)
    count, incoming, outgoing, cached, cost, cost_reports, byte_count = db.execute(statement).one()
    return {
        "period_start": month.isoformat(),
        "visible_events": count,
        "reported_input_tokens": incoming,
        "reported_output_tokens": outgoing,
        "reported_cached_input_tokens": cached,
        "reported_cost_micro_usd": cost if cost_reports else None,
        "cost_report_count": cost_reports,
        "measured_context_bytes": byte_count,
        "scope": "currently-authorized-repositories-and-persons",
        "billing_verified": False,
        "unreported_harness_usage_known": False,
        "budget": budget(db, project_id),
    }


def validate_cases(db, project_id, body):
    for case in body.cases:
        for card_id in case.expected_card_ids:
            card = memory.read_card(db, project_id, card_id, repository_id=body.repository_id)
            if card["state"] != "active":
                raise HubError("EVALUATION_ACTIVE_SOURCE_REQUIRED", 409)


def evaluate_case(db, project_id, body, case):
    response = memory.search(
        db,
        project_id,
        S.MemorySearch(
            query=case.query, repository_id=body.repository_id, search_mode=body.search_mode, limit=body.k
        ),
    )
    ids = [item["id"] for item in response["items"]]
    expected = {str(value) for value in case.expected_card_ids}
    hits = [index + 1 for index, item in enumerate(ids) if item in expected]
    return {
        "query_hash": hashlib.sha256(case.query.encode()).hexdigest(),
        "expected_card_ids": sorted(expected),
        "retrieved_card_ids": ids,
        "recall_at_k": len(hits) / len(expected),
        "reciprocal_rank": 1 / min(hits) if hits else 0,
        "returned_bytes": len(memory.encoded(response)),
        "actual_search_mode": response["search_mode"],
        "semantic_state": response["semantic_search"],
    }


def save_evaluation(hub, db, actor, project_id, body, results, request_id):
    validate_cases(db, project_id, body)
    ids = sorted(
        {
            cid
            for row in results
            for field in ("expected_card_ids", "retrieved_card_ids")
            for cid in row[field]
        }
    )
    # Recheck returned sources after the last unlocked embedding request as well.
    for card_id in ids:
        memory.read_card(db, project_id, uuid.UUID(card_id), repository_id=body.repository_id)
    payload = {
        "schema_version": 1,
        "k": body.k,
        "requested_search_mode": body.search_mode,
        "cases": results,
        "source_card_ids": ids,
        "case_count": len(results),
        "mean_recall_at_k": sum(row["recall_at_k"] for row in results) / len(results),
        "mean_reciprocal_rank": sum(row["reciprocal_rank"] for row in results) / len(results),
        "meaning": "retrieval against supplied labels at this time; not proof of full project understanding",
    }
    row = MemoryEvaluation(
        project_id=project_id,
        repository_id=body.repository_id,
        author_id=actor.user.id,
        device_id=actor.device.id,
        payload=payload,
        content_hash=digest(payload),
    )
    db.add(row)
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "memory.evaluated",
        row.id,
        request_id,
        project_id,
        repository_id=body.repository_id,
        content_hash=row.content_hash,
        case_count=len(results),
    )
    return evaluation_view(row)


def evaluation_view(row):
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat(),
        "content_hash": row.content_hash,
        "payload": row.payload,
        "historical_observation": True,
    }


def install(app, hub, actor, mutate, semantic_query, dependency, security_sessions):
    DB = Annotated[object, Depends(dependency, scope="function")]

    @app.get("/api/projects/{project_id}/usage")
    def usage(project_id: uuid.UUID, request: Request, db: DB, repository_id: uuid.UUID | None = None):
        hub.project(db, actor(request, db), project_id)
        return summary(db, project_id, repository_id)

    @app.put("/api/projects/{project_id}/usage/budget")
    def usage_budget(project_id: uuid.UUID, body: S.UsagePolicyInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: configure(hub, db, identity, project_id, body, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/usage")
    def usage_report(project_id: uuid.UUID, body: S.UsageReport, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:write")
        rate_limit(security_sessions, hub.secret, f"usage-report:{identity.user.id}", 120, 60)
        report_run(db, identity, project_id, body)
        return mutate(
            request,
            db,
            identity,
            body,
            lambda: report(hub, db, identity, project_id, body, request.state.request_id),
        )

    @app.post("/api/projects/{project_id}/memory/evaluations")
    def evaluate(project_id: uuid.UUID, body: S.RetrievalEvaluationInput, request: Request, db: DB):
        identity = actor(request, db)
        hub.project(db, identity, project_id, "tasks:publish")
        rate_limit(security_sessions, hub.secret, f"memory-evaluation:{identity.user.id}", 10, 60)
        validate_cases(db, project_id, body)
        results = []
        for case in body.cases:
            identity = semantic_query(request, db, identity, case.query, body.search_mode)
            hub.project(db, identity, project_id, "tasks:publish")
            validate_cases(db, project_id, body)
            results.append(evaluate_case(db, project_id, body, case))
        result = mutate(
            request,
            db,
            identity,
            body,
            lambda: save_evaluation(hub, db, identity, project_id, body, results, request.state.request_id),
        )
        for card_id in result["payload"]["source_card_ids"]:
            memory.read_card(db, project_id, uuid.UUID(card_id), repository_id=body.repository_id)
        return result

    @app.get("/api/projects/{project_id}/memory/evaluations")
    def evaluations(
        project_id: uuid.UUID,
        request: Request,
        db: DB,
        repository_id: uuid.UUID | None = None,
        limit: int = Query(20, ge=1, le=50),
    ):
        hub.project(db, actor(request, db), project_id, "tasks:publish")
        statement = select(MemoryEvaluation).where(MemoryEvaluation.project_id == project_id)
        if repository_id:
            statement = statement.where(MemoryEvaluation.repository_id == repository_id)
        return {
            "items": [
                evaluation_view(row)
                for row in db.scalars(
                    statement.order_by(MemoryEvaluation.created_at.desc(), MemoryEvaluation.id).limit(limit)
                )
            ]
        }
