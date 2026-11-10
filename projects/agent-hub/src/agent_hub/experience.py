"""Source-authorized experience delivery; engines never grant access or author facts."""

from sqlalchemy import func

from . import knowledge, memory
from .memory_budget import Counter
from .models import MemoryCard, MemoryFact
from .schemas import MemorySearch
from .security import HubError
from .work_policy import digest


def reference(kind, value):
    return ("EXP-" if kind == "knowledge" else "SRC-") + str(value)


def revision(kind, item):
    return digest(
        {
            "kind": kind,
            "id": item["id"],
            "version": item.get("version", item.get("state_version", 0)),
            "source_hash": item["source_hash"],
            "content_hash": item.get("content_hash"),
            "state": item["state"],
        }
    )


def entry(kind, item):
    return {
        "id": item["id"],
        "kind": kind,
        "experience_id": reference(kind, item["id"]),
        "revision": revision(kind, item),
        "version": item.get("version", item.get("state_version", 0)),
        "title": item["title"],
        "body": item["body"],
        "topic": item.get("topic"),
        "source_card_id": item.get("source_card_id", item["id"]),
        "source_hash": item["source_hash"],
        "source": item["source"],
        "additional_source_ids": [c["source_card_id"] for c in item.get("additional_sources", [])],
        "truncated": item.get("truncated", False),
    }


def current(db, project_id, kind, value, repository_id):
    """Inaccessible, withdrawn and nonexistent IDs have the same public result."""
    if kind == "knowledge":
        row = db.execute(
            knowledge.base_query(project_id, repository_id).where(
                MemoryFact.id == value,
                MemoryFact.status == "active",
                func.hub.knowledge_sources_current(MemoryFact.source_card_id, MemoryFact.additional_sources),
            )
        ).first()
        return knowledge.view(db, row, compact=True) if row else None
    row = db.execute(
        memory.base_query(project_id, repository_id).where(
            MemoryCard.id == value, memory.lifecycle_expression() == "active"
        )
    ).first()
    return memory.card_view(db, row) if row else None


class NativeEngine:
    name = "native-source-authorized-v1"

    def candidates(self, db, project_id, body, task):
        if body.experience_ids:
            return [
                ("knowledge", item)
                for value in body.experience_ids
                if (item := current(db, project_id, "knowledge", value, task.repository_id))
            ], {"mode": "authorized-exact-experience-id", "semantic_search": "not-used-exact-id"}
        search = MemorySearch(
            query=body.query or (task.title + " " + task.description)[:200],
            repository_id=task.repository_id,
            search_mode=body.search_mode,
            limit=24,
        )
        facts = knowledge.search(db, project_id, search, compact=True)
        cards = memory.search(db, project_id, search, task=task)
        items = [("knowledge", item) for item in facts["items"]] + [("card", item) for item in cards["items"]]
        return items, {
            "knowledge": {key: facts[key] for key in ("search_mode", "semantic_search", "has_more")},
            "cards": {key: cards[key] for key in ("search_mode", "semantic_search", "has_more")},
        }


def recall(db, project_id, task, body, *, counter=None, engine=None):
    counter, engine = counter or Counter(), engine or NativeEngine()
    result = {
        "schema_version": 1,
        "engine": engine.name,
        "items": [],
        "invalidated": [],
        "unchanged": 0,
        "omitted": 0,
        "gaps": [],
        "content_trust": "untrusted-project-data",
    }
    exhausted = body.consumed_units >= 23000
    maximum = (
        body.max_tokens
        if body.validate_only or exhausted
        else min(body.max_tokens, 24000 - body.consumed_units)
    )
    result["delivery"] = {
        "session_id": str(body.memory_session_id),
        "receipt": digest(body.model_dump(mode="json")),
        "prior_units": body.consumed_units,
        "run_limit": 24000,
        "state": "read-budget-exhausted" if exhausted else "validated" if body.validate_only else "delivered",
        "meter_scope": "delivered-experience-responses; not provider-billing",
    }
    known = {}
    for prior in body.known:
        item = current(db, project_id, prior.kind, prior.id, task.repository_id)
        if not item or revision(prior.kind, item) != prior.revision:
            result["invalidated"].append(reference(prior.kind, prior.id))
        else:
            known[(prior.kind, str(prior.id))] = prior.revision
    if exhausted and not body.validate_only:
        result["gaps"].append("run-read-budget-exhausted; logging and handoff remain available")
    if not body.validate_only and not exhausted:
        candidates, result["retrieval"] = engine.candidates(db, project_id, body, task)
        for kind, item in candidates:
            key = (kind, item["id"])
            if known.get(key) == revision(kind, item):
                result["unchanged"] += 1
                continue
            if len(result["items"]) >= body.limit:
                result["omitted"] += 1
                continue
            result["items"].append(entry(kind, item))
            if not counter.measure(result, maximum) or result["budget"]["serialized_bytes"] > 24000:
                result["items"].pop()
                result["omitted"] += 1
        if result["omitted"]:
            result["gaps"].append("budget-or-candidate-limit; use a focused query or source ID")
        if not result["items"] and not result["unchanged"]:
            result["gaps"].append("no-delivered-match; not proof that no history exists")
    # Metadata, invalidations and diagnostics consume budget too. Never return an oversized first fact.
    while not counter.measure(result, maximum) or result["budget"]["serialized_bytes"] > 24000:
        if result["items"]:
            result["items"].pop()
            result["omitted"] += 1
        else:
            raise HubError("MEMORY_RESPONSE_BUDGET_TOO_SMALL", 422)
    return result
