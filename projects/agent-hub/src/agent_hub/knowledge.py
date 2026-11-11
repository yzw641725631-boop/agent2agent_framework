"""Reviewed, source-bound durable knowledge produced by an authorized local agent or person."""
import uuid
import re
from types import SimpleNamespace

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from . import memory
from .models import MemoryFact, MemoryFactEvent
from .security import HubError
from .work_policy import digest


def state_expression():
    return case((~func.hub.knowledge_sources_current(MemoryFact.source_card_id, MemoryFact.additional_sources), "source_stale"),
                else_=MemoryFact.status)


def base_query(project_id, repository_id=None):
    statement = select(MemoryFact, state_expression().label("state")).where(MemoryFact.project_id == project_id)
    if repository_id:
        statement = statement.where(MemoryFact.resource_id == repository_id)
    return statement.execution_options(populate_existing=True)


def get(db, project_id, fact_id, repository_id=None):
    row = db.execute(base_query(project_id, repository_id).where(MemoryFact.id == fact_id)).first()
    if not row:
        raise HubError("KNOWLEDGE_NOT_FOUND", 404)
    return row


def view(db, row, *, compact=False):
    return views(db, [row], compact=compact)[0]


def views(db, rows, *, compact=False):
    rows = list(rows)
    if not rows:
        return []
    by_project, sources = {}, {}
    for fact, _ in rows:
        ids = by_project.setdefault(fact.project_id, set())
        ids.add(fact.source_card_id)
        ids.update(uuid.UUID(c["source_card_id"]) for c in fact.additional_sources)
    for project_id, ids in by_project.items():
        sources.update(memory.read_cards(db, project_id, ids))
    events = {} if compact else {event.fact_id: event for event in db.scalars(
        select(MemoryFactEvent).where(MemoryFactEvent.fact_id.in_([row[0].id for row in rows]))
        .distinct(MemoryFactEvent.fact_id).order_by(MemoryFactEvent.fact_id, MemoryFactEvent.version.desc())
        .execution_options(populate_existing=True)
    )}
    return [_view(row, sources, events.get(row[0].id), compact=compact) for row in rows]


def _view(row, sources, latest, *, compact):
    fact, state = row
    def source_card(value):
        if str(value) not in sources:
            raise HubError("MEMORY_NOT_FOUND", 404)
        return sources[str(value)]
    source = source_card(fact.source_card_id)
    result = {"id": str(fact.id), "topic": fact.topic, "kind": fact.kind, "title": fact.title, "body": fact.body,
              "state": state, "review_state": fact.status, "version": fact.version, "content_hash": fact.content_hash,
              "source_card_id": str(fact.source_card_id), "source_hash": fact.source_hash, "source": source["source"],
              "line_start": source["line_start"], "line_end": source["line_end"],
              "content_trust": "untrusted-project-data"}
    result["additional_sources"] = [{**citation,
        "source": source_card(citation["source_card_id"])["source"]}
        for citation in fact.additional_sources]
    result["experience_id"] = "EXP-" + str(fact.id)
    result["revision"] = digest({"kind": "knowledge", "id": str(fact.id), "version": fact.version,
                                 "source_hash": fact.source_hash, "content_hash": fact.content_hash, "state": state})
    if compact:
        for citation in result["additional_sources"]:
            citation.pop("source_quote")
    if not compact:
        result.update(source_quote=fact.source_quote, generation=fact.generation, author_id=str(fact.author_id),
                      created_at=fact.created_at.isoformat(), state_reason=latest.reason if latest else "",
                      reviewer_id=str(latest.author_id) if latest and latest.author_id and latest.state != "pending" else None,
                      review_override_reason=latest.override_reason if latest else "",
                      automatic_state_change=latest.automatic if latest else False,
                      generation_usage_trust="author-reported; not provider billing or verified model identity")
    return result


def search(db, project_id, body, *, compact=False):
    from . import semantic
    statement = base_query(project_id, body.repository_id)
    if not body.include_history:
        statement = statement.where(MemoryFact.status == "active",
            func.hub.knowledge_sources_current(MemoryFact.source_card_id, MemoryFact.additional_sources))
    exact = re.fullmatch(r"EXP-([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", body.query.strip(), re.I)
    if exact:
        rows = list(db.execute(statement.where(MemoryFact.id == uuid.UUID(exact[1]))))
        return {"items": views(db, rows[body.offset:body.offset + body.limit], compact=compact),
                "has_more": False, "next_offset": body.offset + len(rows),
                "search_mode": "authorized-exact-experience-id", "semantic_search": "not-used-exact-id",
                "review_required": True, "content_trust": "untrusted-project-data"}
    matches = [func.strpos(func.lower(MemoryFact.topic + "\n" + MemoryFact.title + "\n" + MemoryFact.body), word) > 0
               for word in memory.terms(body.query)]
    score = sum((case((condition, 1), else_=0) for condition in matches), 0)
    hybrid = semantic.hybrid_rows(db, statement, MemoryFact, score, matches, body, kind="fact")
    if hybrid is not None:
        rows, methods, meta = hybrid
        return {"items": [{**item, "retrieved_by": methods[item["id"]]}
                          for item in views(db, rows[:body.limit], compact=compact)], "has_more": len(rows) > body.limit,
                "next_offset": body.offset + min(len(rows), body.limit), **meta,
                "review_required": True, "content_trust": "untrusted-project-data"}
    if matches:
        from sqlalchemy import or_
        statement = statement.where(or_(*matches))
    statement = statement.order_by(score.desc() if not isinstance(score, int) else MemoryFact.created_at.desc(),
                                   MemoryFact.created_at.desc(), MemoryFact.id)
    rows = list(db.execute(statement.offset(body.offset).limit(body.limit + 1)))
    return {"items": views(db, rows[:body.limit], compact=compact),
            "has_more": len(rows) > body.limit, "next_offset": body.offset + min(len(rows), body.limit),
            "search_mode": "authorized-reviewed-knowledge-keywords", "semantic_search": semantic.query_state(db, body).state,
            "review_required": True, "content_trust": "untrusted-project-data"}


def status(db, project_id, repository_id=None):
    rows = base_query(project_id, repository_id).subquery()
    return dict(db.execute(select(rows.c.state, func.count()).group_by(rows.c.state)).all())


def source_for_proposal(db, project_id, body):
    card = memory.read_card(db, project_id, body.source_card_id, repository_id=body.repository_id)
    if card["source_hash"] != body.source_hash:
        raise HubError("KNOWLEDGE_SOURCE_HASH_MISMATCH", 409)
    if not db.scalar(select(func.hub.memory_card_current(body.source_card_id))):
        raise HubError("KNOWLEDGE_SOURCE_STALE", 409)
    if body.source_quote not in card["body"]:
        raise HubError("KNOWLEDGE_QUOTE_MISMATCH", 422)
    for citation in getattr(body, "additional_sources", []):
        source_for_proposal(db, project_id, SimpleNamespace(**citation.model_dump(), repository_id=None))
    return card


def append_event(db, fact, state, reason, actor, override_reason=""):
    fact.status, fact.version = state, fact.version + 1
    db.add(MemoryFactEvent(fact_id=fact.id, project_id=fact.project_id, version=fact.version, state=state,
                          reason=reason, author_id=actor.user.id, device_id=actor.device.id,
                          override_reason=override_reason))


def propose(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:write")
    source_for_proposal(db, project_id, body)
    resource = db.scalar(select(func.hub.memory_card_resource(body.source_card_id, project_id)))
    values = body.model_dump(mode="python", exclude={"repository_id", "reason", "generation"})
    values["generation"] = body.generation.model_dump(mode="json")
    values["additional_sources"] = [citation.model_dump(mode="json") for citation in body.additional_sources]
    payload = {**body.model_dump(mode="json", exclude={"repository_id", "reason"}), "resource_id": str(resource)}
    if not body.additional_sources:
        payload.pop("additional_sources")  # Preserve the original single-source digest contract.
    fact = MemoryFact(project_id=project_id, author_id=actor.user.id, device_id=actor.device.id,
                      resource_id=resource, content_hash=digest(payload), status="pending", version=1, **values)
    db.add(fact)
    db.flush()
    db.add(MemoryFactEvent(fact_id=fact.id, project_id=project_id, version=1, state="pending", reason=body.reason,
                          author_id=actor.user.id, device_id=actor.device.id))
    db.flush()
    hub.audit(db, actor.user.id, "knowledge.proposed", fact.id, request_id, project_id,
              repository_id=str(resource) if resource != project_id else None)
    return view(db, get(db, project_id, fact.id, body.repository_id))


def decide(hub, db, actor, project_id, fact_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    fact, _ = get(db, project_id, fact_id, body.repository_id)
    if fact.version != body.expected_version:
        raise HubError("KNOWLEDGE_VERSION_CONFLICT", 409, current_version=fact.version)
    if fact.status == body.state:
        raise HubError("KNOWLEDGE_STATE_UNCHANGED", 409)
    replacement = None
    if body.state == "active":
        if not db.scalar(select(func.hub.knowledge_sources_current(
                MemoryFact.source_card_id, MemoryFact.additional_sources)).where(MemoryFact.id == fact.id)):
            raise HubError("KNOWLEDGE_SOURCE_STALE", 409)
        if fact.author_id == actor.user.id:
            if actor.user.platform_role != "superadmin" or len(body.override_reason.strip()) < 10:
                raise HubError("KNOWLEDGE_INDEPENDENT_REVIEW_REQUIRED", 403)
        elif body.override_reason:
            raise HubError("KNOWLEDGE_OVERRIDE_NOT_APPLICABLE", 422)
        if body.replaces_id:
            replacement, _ = get(db, project_id, body.replaces_id, body.repository_id)
            if replacement.id == fact.id or replacement.resource_id != fact.resource_id or replacement.topic != fact.topic:
                raise HubError("KNOWLEDGE_REPLACEMENT_MISMATCH", 409)
            if replacement.version != body.replaces_version or replacement.status != "active":
                raise HubError("KNOWLEDGE_VERSION_CONFLICT", 409)
    elif body.override_reason:
        raise HubError("KNOWLEDGE_OVERRIDE_NOT_APPLICABLE", 422)
    try:
        with db.begin_nested():
            if replacement:
                append_event(db, replacement, "superseded", body.reason, actor)
                db.flush()
            append_event(db, fact, body.state, body.reason, actor, body.override_reason)
            db.flush()
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "one_active_memory_fact_topic":
            # An inaccessible competing fact must not leak its ID, author, title or count.
            raise HubError("KNOWLEDGE_TOPIC_CONFLICT", 409) from None
        raise
    hub.audit(db, actor.user.id, "knowledge." + body.state, fact.id, request_id, project_id,
              version=fact.version, replaces_id=str(replacement.id) if replacement else None)
    return view(db, get(db, project_id, fact.id, body.repository_id))


def validate_bundle(db, project_id, bundle):
    for item in bundle.get("knowledge", []):
        try:
            fact, state = get(db, project_id, uuid.UUID(item["id"]))
        except HubError:
            raise HubError("WORK_MEMORY_ACCESS_CHANGED", 409) from None
        if state != "active" or fact.version != item["version"] or fact.content_hash != item["content_hash"]:
            raise HubError("WORK_MEMORY_STALE", 409)
