"""Pinned local embeddings and authorized exact pgvector / literal rank fusion."""

import hashlib
import json
import math
from dataclasses import dataclass

import httpx
from sqlalchemy import Float, bindparam, cast, func, literal, or_, select
from sqlalchemy.types import UserDefinedType

MODEL = "bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1"
DIMENSIONS = 512
MAX_CANDIDATES = 240


def content(title, body, topic=""):
    return (topic + "\n" if topic else "") + title + "\n" + body


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def vector_literal(vector):
    if not isinstance(vector, (list, tuple)) or len(vector) != DIMENSIONS:
        raise ValueError("Embedding dimension mismatch")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
        raise ValueError("Embedding contains invalid values")
    norm = sum(value * value for value in vector)
    if abs(norm - 1) > 0.01:
        raise ValueError("Embedding is not normalized")
    return json.dumps(vector, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class QueryVector:
    query_hash: str = ""
    vector: tuple = ()
    state: str = "not-configured"


class Embeddings:
    def __init__(self, settings, *, transport=None):
        self.url = settings.embedding_url
        self.transport = transport

    def encode(self, value, kind):
        if not self.url:
            raise ValueError("Embedding not configured")
        # The URL is operator-configured and restricted to the private service.
        with httpx.Client(
            timeout=httpx.Timeout(4, connect=1),
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            with client.stream(
                "POST", self.url + "/embed", json={"model": MODEL, "kind": kind, "text": value}
            ) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 30000:
                        raise ValueError("Embedding response too large")
        body = json.loads(data)
        if body["model"] != MODEL or body["dimensions"] != DIMENSIONS:
            raise ValueError("Embedding model mismatch")
        vector_literal(body["vector"])
        return tuple(body["vector"])

    def query(self, value):
        if not self.url:
            return QueryVector(state="not-configured")
        if not value.strip():
            return QueryVector(state="browse")
        try:
            return QueryVector(digest(value), self.encode(value, "query"), "ready")
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            # Do not log query text, source text or provider responses.
            return QueryVector(digest(value), state="unavailable-keyword-fallback")


def query_state(db, body):
    if body.search_mode == "keyword":
        return QueryVector(state="keyword-requested")
    vector = db.info.get("semantic_query", QueryVector())
    return (
        vector
        if vector.query_hash == digest(body.query) or not vector.vector
        else QueryVector(state="query-changed-keyword-fallback")
    )


def embedding_table(kind):
    from .models import KnowledgeEmbedding, MemoryEmbedding

    return MemoryEmbedding if kind == "card" else KnowledgeEmbedding


def hybrid_rows(db, statement, entity, score, matches, body, *, kind, priority=None):
    """Rank only rows visible through the caller's original-source RLS.

    Exact search deliberately has no ANN index. Both lists are capped; paging
    reports this bound rather than implying a complete semantic enumeration.
    """
    query = query_state(db, body)
    if not query.vector or not body.query.strip():
        return None
    table = embedding_table(kind)
    key = table.card_id if kind == "card" else table.fact_id
    topic = entity.topic if kind == "fact" else literal("")
    source_hash = func.encode(
        func.sha256(
            func.convert_to(
                (topic + "\n" if kind == "fact" else literal("")) + entity.title + "\n" + entity.body, "UTF8"
            )
        ),
        "hex",
    )
    # Cast uses a bound value; embeddings supplied by clients are never accepted.
    distance = cast(
        table.embedding.op("<=>")(cast(bindparam("query_vector", vector_literal(query.vector)), Vector())),
        Float,
    )
    conditions = [
        key == entity.id,
        table.model == MODEL,
        table.dimensions == DIMENSIONS,
        table.content_hash == source_hash,
    ]
    semantic = list(
        db.execute(
            statement.join(table, conditions[0])
            .where(*conditions[1:])
            .order_by(distance, entity.id)
            .limit(MAX_CANDIDATES)
        )
    )
    literal_query = statement.where(or_(*matches)) if matches else statement
    if priority is not None:
        literal_query = statement.where(or_(priority, *matches))
    literal_rows = list(
        db.execute(
            literal_query.order_by(
                (literal(score) if isinstance(score, int) else score).desc(),
                entity.created_at.desc(),
                entity.id,
            ).limit(MAX_CANDIDATES)
        )
    )
    ranks, rows, methods = {}, {}, {}
    for method, candidates in (("keyword", literal_rows), ("semantic", semantic)):
        for rank, row in enumerate(candidates, 1):
            item_id = row[0].id
            rows[item_id] = row
            ranks[item_id] = ranks.get(item_id, 0) + 1 / (60 + rank)
            methods.setdefault(item_id, []).append(method)
    # Current-task handoff remains first; it does not depend on embedding latency.
    ordered = sorted(
        rows,
        key=lambda key: (
            -int(
                priority is not None
                and getattr(rows[key][1], "task_id", None) == db.info.get("semantic_task_id")
            ),
            -ranks[key],
            str(key),
        ),
    )
    selected = ordered[body.offset : body.offset + body.limit + 1]
    metadata = {
        "search_mode": "authorized-exact-vector-keyword-rrf",
        "semantic_search": "ready",
        "embedding_model": MODEL,
        "dimensions": DIMENSIONS,
        "candidate_limit_per_method": MAX_CANDIDATES,
        "candidate_limit_reached": len(semantic) == MAX_CANDIDATES or len(literal_rows) == MAX_CANDIDATES,
        "ranking_meaning": "relative reciprocal ranks; not relevance probabilities or verified facts",
    }
    return [rows[key] for key in selected], {str(key): methods[key] for key in selected}, metadata


# Keep pgvector optional in the Python runtime; PostgreSQL validates the actual type.
class Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_):
        return "vector"


def coverage(db, statement, entity, kind):
    table = embedding_table(kind)
    key = table.card_id if kind == "card" else table.fact_id
    topic = entity.topic + "\n" if kind == "fact" else literal("")
    expected = func.encode(
        func.sha256(func.convert_to(topic + entity.title + "\n" + entity.body, "UTF8")), "hex"
    )
    visible = statement.with_only_columns(entity.id, expected.label("expected")).order_by(None).subquery()
    total = db.scalar(select(func.count()).select_from(visible))
    indexed = db.scalar(
        select(func.count())
        .select_from(visible)
        .join(table, key == visible.c.id)
        .where(table.model == MODEL, table.dimensions == DIMENSIONS, table.content_hash == visible.c.expected)
    )
    return {"visible": total, "indexed": indexed, "pending_or_failed": total - indexed}
