"""Source-bound, extractive memory. No document text becomes execution authority."""
import hashlib
import json
import re
import uuid

from sqlalchemy import case, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from .models import (
    DocumentVersion,
    MemoryCard,
    MemoryEvent,
    MemoryJob,
    ProjectDocument,
    Task,
    Worklog,
)
from .security import HubError

EXTRACTOR = "extractive-v1"
DEFAULT_BUDGET = 12000


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def clipped(value, maximum):
    raw = value.encode()
    return (raw[:maximum].decode("utf-8", errors="ignore"), len(raw) > maximum)


def archive_document(db, repository, snapshot_id, commit, doc):
    original = doc.get("original_bytes")
    exact = original is not None and hashlib.sha256(original).hexdigest() == doc["sha256"]
    if original is not None and not exact:
        raise HubError("DOCUMENT_ORIGINAL_HASH_MISMATCH", 422)
    key = dict(source_id=repository.id, configuration_version=repository.version,
               path=doc["path"], sha256=doc["sha256"])
    db.execute(insert(DocumentVersion).values(
        id=uuid.uuid4(), project_id=repository.project_id, snapshot_id=snapshot_id,
        repository_url=repository.repository_url, branch=repository.branch, git_commit=commit,
        title=doc["title"], content=doc["content"], content_hash=hashlib.sha256(doc["content"].encode()).hexdigest(),
        original_bytes=original, exact_bytes=exact, **key,
    ).on_conflict_do_nothing(index_elements=list(key)))
    return db.scalar(select(DocumentVersion).filter_by(**key))


def extract_log(row):
    # Keep tests and the next action separately: neither is inferred from prose.
    parts, truncated = [], False
    for label, value, budget in (("工作", row.summary, 900), ("说明", row.notes, 1600),
                                  ("验证（作者报告）", row.tests, 800), ("下一步", row.next_steps, 800)):
        excerpt, cut = clipped(value, budget)
        parts.append(f"{label}：{excerpt}")
        truncated |= cut
    return [dict(ordinal=0, title=row.summary, body="\n".join(parts), truncated=truncated,
                 source_hash=row.receipt_hash, line_start=None, line_end=None)]


def extract_document(row):
    # Chunks retain exact line ranges. Very long single lines require explicit original reads.
    result, lines, start, size, heading = [], [], 1, 0, row.title

    def emit(end):
        if not lines:
            return
        body, cut = clipped("\n".join(lines), 5000)
        result.append(dict(ordinal=len(result), title=heading[:300], body=body, truncated=cut,
                           source_hash=row.content_hash, line_start=start, line_end=end))

    for number, line in enumerate(row.content.splitlines(), 1):
        section = re.match(r"^#{1,6}\s+(.+)", line)
        if lines and (section or size + len(line.encode()) > 4500):
            emit(number - 1)
            lines, start, size = [], number, 0
        if section:
            heading = section[1].strip()
        lines.append(line)
        size += len(line.encode()) + 1
    emit(start + len(lines) - 1)
    if not result:
        result.append(dict(ordinal=0, title=row.title, body="（空文档）", truncated=False,
                           source_hash=row.content_hash, line_start=1, line_end=1))
    return result


def lifecycle_expression():
    latest = (select(MemoryEvent.state).where(MemoryEvent.card_id == MemoryCard.id)
              .order_by(MemoryEvent.version.desc()).limit(1).correlate(MemoryCard).scalar_subquery())
    newer = aliased(Worklog)
    superseded_log = exists(select(newer.id).where(
        newer.task_id == Worklog.task_id, newer.project_id == Worklog.project_id,
        tuple_(newer.created_at, newer.id) > tuple_(Worklog.created_at, Worklog.id),
    )).correlate(Worklog)
    current_document = exists(select(ProjectDocument.path).where(
        ProjectDocument.version_id == MemoryCard.document_id,
    )).correlate(MemoryCard)
    return case((latest.in_(["conflicted", "retired"]), latest),
                (MemoryCard.worklog_id.is_not(None) & superseded_log, "superseded"),
                (MemoryCard.document_id.is_not(None) & ~current_document, "superseded"), else_="active")


def base_query(project_id, repository_id=None):
    statement = (select(MemoryCard, Worklog, DocumentVersion, Task.repository_id, lifecycle_expression().label("state"))
                 .outerjoin(Worklog, Worklog.id == MemoryCard.worklog_id)
                 .outerjoin(Task, Task.id == Worklog.task_id)
                 .outerjoin(DocumentVersion, DocumentVersion.id == MemoryCard.document_id)
                 .where(MemoryCard.project_id == project_id))
    if repository_id:
        statement = statement.where(or_(Task.repository_id == repository_id, DocumentVersion.source_id == repository_id))
    return statement


def source_reference(log, doc, repository_id):
    if log:
        return {"kind": "worklog", "id": str(log.id), "task_id": str(log.task_id),
                "repository_id": str(repository_id) if repository_id else None,
                "git_commit": log.git_commit, "receipt_hash": log.receipt_hash,
                "created_at": log.created_at.isoformat(), "outcome": log.outcome}
    return {"kind": "document", "id": str(doc.id), "source_id": str(doc.source_id),
            "repository_id": str(doc.source_id) if doc.source_id != doc.project_id else None,
            "path": doc.path, "git_commit": doc.git_commit, "snapshot_id": str(doc.snapshot_id),
            "sha256": doc.sha256, "content_hash": doc.content_hash, "exact_bytes": doc.exact_bytes,
            "reference_meaning": "first archived occurrence of this content in this configuration"}


def card_view(db, row):
    card, log, doc, rid, state = row
    event = db.scalar(select(MemoryEvent).where(MemoryEvent.card_id == card.id)
                      .order_by(MemoryEvent.version.desc()).limit(1))
    return {"id": str(card.id), "title": card.title, "body": card.body,
            "state": state, "state_version": event.version if event else 0,
            "state_reason": event.reason if event else "",
            "extractor": card.extractor, "source": source_reference(log, doc, rid),
            "source_hash": card.source_hash, "line_start": card.line_start, "line_end": card.line_end,
            "truncated": card.truncated, "content_trust": "untrusted-project-data"}


def terms(query):
    words = re.findall(r"[a-zA-Z0-9_./-]{2,}|[\u3400-\u9fff]+", query.lower())
    result = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 2:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return list(dict.fromkeys(result))[:20]


def search(db, project_id, body, *, task=None):
    statement = base_query(project_id, body.repository_id)
    state = lifecycle_expression()
    if not body.include_history:
        statement = statement.where(state == "active")
    keywords = terms(body.query)
    matches = [func.strpos(func.lower(MemoryCard.title + "\n" + MemoryCard.body), word) > 0 for word in keywords]
    score = sum((case((condition, 1), else_=0) for condition in matches), 0)
    if task:
        score = score + case((Worklog.task_id == task.id, 100), else_=0)
        if matches:
            statement = statement.where(or_(Worklog.task_id == task.id, *matches))
    elif matches:
        statement = statement.where(or_(*matches))
    statement = statement.order_by(score.desc() if not isinstance(score, int) else MemoryCard.created_at.desc(),
                                   MemoryCard.created_at.desc(), MemoryCard.id, MemoryCard.ordinal)
    rows = list(db.execute(statement.offset(body.offset).limit(body.limit + 1)))
    items = [card_view(db, row) for row in rows[:body.limit]]
    return {"items": items, "has_more": len(rows) > body.limit, "next_offset": body.offset + len(items),
            "search_mode": "authorized-literal-keywords", "semantic_search": "not-configured",
            "content_trust": "untrusted-project-data"}


def read_card(db, project_id, card_id, *, repository_id=None):
    row = db.execute(base_query(project_id, repository_id).where(MemoryCard.id == card_id)).first()
    if not row:
        raise HubError("MEMORY_NOT_FOUND", 404)
    return card_view(db, row)


def status(db, project_id, repository_id=None):
    from . import knowledge
    # Count only rows whose original source is currently visible to this session.
    cards = base_query(project_id, repository_id).subquery()
    counts = dict(db.execute(select(cards.c.state, func.count()).group_by(cards.c.state)).all())
    jobs = select(MemoryJob.status, func.count()).where(MemoryJob.project_id == project_id)
    if repository_id:
        jobs = (jobs.outerjoin(Worklog, Worklog.id == MemoryJob.worklog_id)
                .outerjoin(Task, Task.id == Worklog.task_id)
                .outerjoin(DocumentVersion, DocumentVersion.id == MemoryJob.document_id)
                .where(or_(Task.repository_id == repository_id, DocumentVersion.source_id == repository_id)))
    queue = dict(db.execute(jobs.group_by(MemoryJob.status)).all())
    return {"mode": EXTRACTOR, "automatic_extraction": True, "semantic_distillation": False,
            "knowledge": knowledge.status(db, project_id, repository_id), "knowledge_review_required": True,
            "semantic_search": "not-configured", "visible_cards": counts, "visible_jobs": queue,
            "source_visibility": "current-original-source-permissions", "vector_index": "provisioned-empty"}


def prepare_bundle(db, project_id, task, budget=DEFAULT_BUDGET):
    from . import knowledge
    from .schemas import MemorySearch
    selected = search(db, project_id, MemorySearch(query=(task.title + " " + task.description)[:200],
                                                   repository_id=task.repository_id, limit=24), task=task)
    facts = knowledge.search(db, project_id, MemorySearch(query=(task.title + " " + task.description)[:200],
                                                       repository_id=task.repository_id, limit=12), compact=True)
    result = {"schema_version": 2, "mode": EXTRACTOR, "items": [], "knowledge": [], "recent_unindexed": [],
              "byte_budget": budget, "budget_measure": "serialized-utf8-bytes; model token count is not measured",
              "omitted_candidates": 0, "coverage": status(db, project_id, task.repository_id),
              "content_trust": "untrusted-project-data", "gaps": []}
    # Fresh logs remain available even before the asynchronous worker has run.
    logs = list(db.scalars(select(Worklog).where(
        Worklog.project_id == project_id, Worklog.task_id == task.id,
        ~exists(select(MemoryCard.id).where(MemoryCard.worklog_id == Worklog.id)),
    ).order_by(Worklog.created_at.desc(), Worklog.id.desc()).limit(3)))
    candidates = [("recent_unindexed", {"id": str(log.id), "summary": clipped(log.summary, 900)[0],
                                        "next_steps": clipped(log.next_steps, 900)[0],
                                        "receipt_hash": log.receipt_hash}) for log in logs]
    candidates.extend(("knowledge", fact) for fact in facts["items"])
    candidates.extend(("items", card) for card in selected["items"])
    for target, item in candidates:
        result[target].append(item)
        if len(encoded(result)) + 300 > budget:
            result[target].pop()
            result["omitted_candidates"] += 1
    if selected["has_more"] or facts["has_more"] or result["omitted_candidates"]:
        result["gaps"].append("budget-or-candidate-limit: search and read original sources when needed")
    if result["coverage"]["visible_jobs"].get("queued", 0):
        result["gaps"].append("extraction-pending: current-task fresh logs included separately")
    if result["coverage"]["visible_jobs"].get("failed", 0):
        result["gaps"].append("extraction-failed: use original document/log tools")
    if not result["items"] and not result["knowledge"] and not result["recent_unindexed"]:
        result["gaps"].append("no-matching-visible-memory: do not infer that no coworker history exists")
    result["serialized_bytes"] = len(encoded(result)) + 32
    return result


def validate_bundle(db, project_id, payload):
    """New history does not invalidate an active run; lost authority and retired facts do."""
    bundle = payload.get("memory", {})
    from . import knowledge
    knowledge.validate_bundle(db, project_id, bundle)
    for item in bundle.get("items", []):
        try:
            current = read_card(db, project_id, uuid.UUID(item["id"]))
        except HubError:
            raise HubError("WORK_MEMORY_ACCESS_CHANGED", 409) from None
        if current["state"] in {"retired", "conflicted"} or (
                current["source"]["kind"] == "document" and current["state"] != "active"):
            raise HubError("WORK_MEMORY_STALE", 409)
    for item in bundle.get("recent_unindexed", []):
        if not db.get(Worklog, uuid.UUID(item["id"])):
            raise HubError("WORK_MEMORY_ACCESS_CHANGED", 409)


def decide(hub, db, actor, project_id, card_id, body, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    card = read_card(db, project_id, card_id)
    if card["state_version"] != body.expected_version:
        raise HubError("MEMORY_VERSION_CONFLICT", 409, current_version=card["state_version"])
    event = MemoryEvent(card_id=card_id, project_id=project_id, version=body.expected_version + 1,
                        author_id=actor.user.id, device_id=actor.device.id, state=body.state, reason=body.reason)
    db.add(event)
    db.flush()
    hub.audit(db, actor.user.id, "memory." + body.state, card_id, request_id, project_id,
              repository_id=card["source"]["repository_id"], version=event.version)
    return read_card(db, project_id, card_id)


def retry_failed(hub, db, actor, project_id, request_id):
    hub.project(db, actor, project_id, "tasks:publish")
    result = db.execute(update(MemoryJob).where(MemoryJob.project_id == project_id, MemoryJob.status == "failed")
                        .values(status="queued", attempts=0, error_code=None, finished_at=None))
    hub.audit(db, actor.user.id, "memory.retry_requested", project_id, request_id, project_id, count=result.rowcount)
    return {"queued": result.rowcount}


def original(db, project_id, card_id, start_line, limit, *, repository_id=None, start_char=0):
    card = read_card(db, project_id, card_id, repository_id=repository_id)
    reference = card["source"]
    if reference["kind"] == "document":
        doc = db.get(DocumentVersion, uuid.UUID(reference["id"]))
        content = doc.content
    else:
        log = db.get(Worklog, uuid.UUID(reference["id"]))
        content = "\n".join((log.summary, log.notes, "验证：" + log.tests, "下一步：" + log.next_steps))
    lines = content.splitlines()
    selected = lines[start_line - 1:start_line - 1 + limit]
    if selected:
        if start_char > len(selected[0]):
            raise HubError("MEMORY_SOURCE_CURSOR_INVALID", 422)
        selected[0] = selected[0][start_char:]
    excerpt, cut = clipped("\n".join(selected), 16000)
    consumed_lines = excerpt.count("\n")
    next_line = start_line + consumed_lines if cut else start_line + len(selected)
    next_char = ((start_char if not consumed_lines else 0) + len(excerpt.rsplit("\n", 1)[-1])) if cut else 0
    return {"card_id": str(card_id), "source": reference, "content": excerpt,
            "start_line": start_line, "start_char": start_char, "next_start_line": next_line,
            "next_start_char": next_char, "line_count": len(lines), "truncated": cut,
            "has_more": cut or start_line - 1 + limit < len(lines), "content_trust": "untrusted-project-data"}
