"""Lease a job, infer outside the transaction, then verify and atomically store."""

import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import and_, or_, select, text

from .db import session_factory
from .models import EmbeddingJob, MemoryCard, MemoryFact
from .security import now
from .semantic import DIMENSIONS, MODEL, Embeddings, content, digest, vector_literal

log = logging.getLogger(__name__)


class EmbeddingWorker:
    def __init__(self, settings, *, factory=None, embeddings=None):
        self.sessions = factory or session_factory(settings, security=True)
        self.embeddings = embeddings or Embeddings(settings)

    def source(self, db, job):
        row = db.get(MemoryCard, job.card_id) if job.card_id else db.get(MemoryFact, job.fact_id)
        if not row or (job.fact_id and row.status != "active"):
            return None
        return content(row.title, row.body, row.topic if job.fact_id else "")

    def run_once(self):
        with self.sessions.begin() as db:
            job = db.scalar(
                select(EmbeddingJob)
                .where(
                    EmbeddingJob.model == MODEL,
                    or_(
                        and_(EmbeddingJob.status == "queued", EmbeddingJob.not_before <= now()),
                        and_(EmbeddingJob.status == "running", EmbeddingJob.lease_until < now()),
                    ),
                )
                .order_by(EmbeddingJob.created_at, EmbeddingJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not job:
                return False
            source = self.source(db, job)
            if source is None or digest(source) != job.content_hash:
                job.status, job.error_code, job.finished_at = "skipped", "EMBEDDING_SOURCE_CHANGED", now()
                return True
            job.attempts += 1
            job.status, job.lease_token, job.lease_until = (
                "running",
                uuid.uuid4(),
                now() + timedelta(seconds=45),
            )
            job_id, lease, expected = job.id, job.lease_token, job.content_hash
        # No advisory locks or database connection held during model inference.
        vector, error = None, None
        try:
            vector = vector_literal(self.embeddings.encode(source, "passage"))
        except Exception:
            error = "EMBEDDING_INFERENCE_FAILED"
        with self.sessions.begin() as db:
            job = db.scalar(select(EmbeddingJob).where(EmbeddingJob.id == job_id).with_for_update())
            if not job or job.status != "running" or job.lease_token != lease or job.lease_until <= now():
                return True
            current = self.source(db, job)
            if current is None or digest(current) != expected:
                job.status, job.error_code, job.finished_at = "skipped", "EMBEDDING_SOURCE_CHANGED", now()
            elif error:
                job.error_code = error
                job.status = "failed" if job.attempts >= 3 else "queued"
                job.finished_at = now() if job.status == "failed" else None
                job.not_before = now() + timedelta(seconds=30 * job.attempts)
                log.error("Embedding deferred; original retained")
            else:
                table, key, item_id = (
                    ("memory_embeddings", "card_id", job.card_id)
                    if job.card_id
                    else ("knowledge_embeddings", "fact_id", job.fact_id)
                )
                db.execute(
                    text(f"""INSERT INTO {table}({key},model,dimensions,content_hash,embedding)
                    VALUES(:id,:model,:dim,:hash,CAST(:vector AS vector))
                    ON CONFLICT({key},model) DO UPDATE SET dimensions=excluded.dimensions,
                      content_hash=excluded.content_hash,embedding=excluded.embedding"""),
                    {"id": item_id, "model": MODEL, "dim": DIMENSIONS, "hash": expected, "vector": vector},
                )
                job.status, job.error_code, job.finished_at = "succeeded", None, now()
            job.lease_until, job.lease_token = None, None
        return True

    def serve(self):
        if not self.embeddings.url:
            raise RuntimeError("Embedding worker requires the private model service")
        with self.sessions() as db:
            if not db.scalar(text("SELECT pg_has_role(current_user,'hub_embedding','member')")):
                raise RuntimeError("Embedding worker requires the separate hub_embedding database role")
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.error("Embedding job deferred; original retained")
                worked = False
            if not worked:
                time.sleep(2)
