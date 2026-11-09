"""Offline archive verification after backup restore, under the database owner."""
import hashlib

from sqlalchemy import select, text

from .models import DocumentVersion, MemoryCard, Worklog


def audit(factory):
    result = {"documents": 0, "exact_originals": 0, "cards": 0, "errors": [], "healthy": True}
    with factory() as db:
        owner = db.scalar(text("SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='public.document_versions'::regclass"))
        if not owner:
            raise RuntimeError("Memory archive verification requires the migration owner")
        documents = {}
        for doc in db.scalars(select(DocumentVersion).execution_options(yield_per=50)):
            result["documents"] += 1
            actual = hashlib.sha256(doc.content.encode()).hexdigest()
            documents[doc.id] = doc.content_hash
            if actual != doc.content_hash:
                result["errors"].append({"kind": "document", "id": str(doc.id), "code": "CONTENT_HASH_MISMATCH"})
            if doc.exact_bytes:
                result["exact_originals"] += 1
                if doc.original_bytes is None or hashlib.sha256(doc.original_bytes).hexdigest() != doc.sha256:
                    result["errors"].append({"kind": "document", "id": str(doc.id), "code": "ORIGINAL_HASH_MISMATCH"})
        db.execute(text("SET LOCAL ROLE hub_memory"))
        logs = dict(db.execute(select(Worklog.id, Worklog.receipt_hash)).all())
        for card in db.scalars(select(MemoryCard).execution_options(yield_per=200)):
            result["cards"] += 1
            expected = logs.get(card.worklog_id) if card.worklog_id else documents.get(card.document_id)
            if expected != card.source_hash:
                result["errors"].append({"kind": "card", "id": str(card.id), "code": "SOURCE_HASH_MISMATCH"})
    result["healthy"] = not result["errors"]
    return result
