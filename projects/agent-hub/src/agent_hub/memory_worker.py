"""Atomic incremental extraction under a dedicated role; no network or model credentials."""
import logging
import time

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .db import session_factory
from .memory import EXTRACTOR, extract_document, extract_log
from .models import DocumentVersion, MemoryCard, MemoryJob, Worklog
from .security import now

log = logging.getLogger(__name__)


class MemoryWorker:
    def __init__(self, settings, *, factory=None):
        self.sessions = factory or session_factory(settings, security=True)
        self.last_scheduled = 0
        self.sources_changed = True

    def run_once(self):
        with self.sessions.begin() as db:
            job = db.scalar(select(MemoryJob).where(MemoryJob.status == "queued")
                            .order_by(MemoryJob.created_at, MemoryJob.id).limit(1).with_for_update(skip_locked=True))
            if not job:
                if not self.sources_changed and time.monotonic() - self.last_scheduled < 30:
                    return False
                self.last_scheduled, self.sources_changed = time.monotonic(), False
                return bool(db.scalar(text("SELECT hub.schedule_distillation()")))
            job.attempts += 1
            try:
                with db.begin_nested():
                    source = db.get(Worklog, job.worklog_id) if job.worklog_id else db.get(DocumentVersion, job.document_id)
                    cards = extract_log(source) if job.worklog_id else extract_document(source)
                    for card in cards:
                        db.execute(insert(MemoryCard).values(
                            project_id=job.project_id, worklog_id=job.worklog_id, document_id=job.document_id,
                            extractor=EXTRACTOR, **card,
                        ).on_conflict_do_nothing())
                job.status, job.finished_at, job.error_code = "succeeded", now(), None
                self.sources_changed = True
            except Exception:
                job.error_code = "MEMORY_EXTRACTION_FAILED"
                if job.attempts >= 3:
                    job.status, job.finished_at = "failed", now()
                log.error("Memory extraction failed; source retained")
        return True

    def serve(self):
        with self.sessions() as db:
            if not db.scalar(text("SELECT pg_has_role(current_user,'hub_memory','member')")):
                raise RuntimeError("Memory worker requires the separate hub_memory database role")
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.error("Memory worker transaction failed; source retained")
                worked = False
            if not worked:
                time.sleep(2)
