"""Git is authoritative; PostgreSQL stores replaceable, commit-addressed Markdown snapshots."""

import base64
import hashlib
import re

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, func, or_, select, text, update

from .models import DocumentRepository, DocumentSync, ProjectDocument
from .security import HubError, now


def token_cipher(settings):
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-hub.repository-credential.v1"
    ).derive(settings.secret.get_secret_value().encode())
    return Fernet(base64.urlsafe_b64encode(key))


def require_manager(hub, db, actor, project_id):
    hub.project(db, actor, project_id, "documents:manage")
    if not db.scalar(text("SELECT hub.can_manage_documents(:p)"), {"p": project_id}):
        raise HubError("PROJECT_NOT_ACCESSIBLE", 404)


def job_view(job):
    if not job:
        return None
    return {
        "id": str(job.id),
        "status": job.status,
        "attempts": job.attempts,
        "configuration_version": job.configuration_version,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_code": job.error_code,
    }


def repository_view(db, project_id, *, manager=False):
    row = db.get(DocumentRepository, project_id)
    if not row:
        return {"configured": False, "version": 0, "can_manage": manager}
    result = {
        "configured": True,
        "version": row.version,
        "can_manage": manager,
        "repository_url": row.repository_url,
        "branch": row.branch,
        "include_paths": row.include_paths,
        "snapshot_id": str(row.snapshot_id) if row.snapshot_id else None,
        "git_commit": row.git_commit,
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
        "document_count": row.document_count,
        "skipped_count": row.skipped_count,
        "search_mode": "literal-substring",
    }
    if manager:
        job = db.scalar(
            select(DocumentSync)
            .where(DocumentSync.project_id == project_id)
            .order_by(DocumentSync.created_at.desc())
            .limit(1)
        )
        result.update(token_set=bool(row.encrypted_token), latest_sync=job_view(job))
    return result


def configure(hub, db, actor, project_id, body, request_id):
    row = db.get(DocumentRepository, project_id)
    if body.expected_version != (row.version if row else 0):
        raise HubError("CONFIGURATION_VERSION_CONFLICT", 409)
    if body.token is not None and body.clear_token:
        raise HubError("INVALID_REQUEST", 422)
    encrypted = row.encrypted_token if row else None
    if (
        row
        and row.repository_url != body.repository_url
        and encrypted
        and body.token is None
        and not body.clear_token
    ):
        raise HubError("REPOSITORY_CREDENTIAL_CONFIRMATION_REQUIRED", 422)
    if body.token is not None:
        encrypted = token_cipher(hub.settings).encrypt(body.token.get_secret_value().encode()).decode()
    elif body.clear_token:
        encrypted = None
    # Configuration changes invalidate all old content in the same transaction.
    db.execute(delete(ProjectDocument).where(ProjectDocument.project_id == project_id))
    db.execute(
        update(DocumentSync)
        .where(DocumentSync.project_id == project_id, DocumentSync.status.in_(["queued", "running"]))
        .values(status="cancelled", finished_at=now())
    )
    if row is None:
        row = DocumentRepository(project_id=project_id)
        db.add(row)
    row.repository_url, row.branch, row.include_paths = body.repository_url, body.branch, body.include_paths
    row.encrypted_token, row.version = encrypted, body.expected_version + 1
    row.snapshot_id = row.git_commit = row.indexed_at = None
    row.document_count = row.skipped_count = 0
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "documents.configured",
        project_id,
        request_id,
        project_id,
        configuration_version=row.version,
    )
    return repository_view(db, project_id, manager=True)


def enqueue(hub, db, actor, project_id, request_id):
    row = db.get(DocumentRepository, project_id)
    if not row:
        raise HubError("DOCUMENT_REPOSITORY_NOT_CONFIGURED", 409)
    job = db.scalar(
        select(DocumentSync).where(
            DocumentSync.project_id == project_id, DocumentSync.status.in_(["queued", "running"])
        )
    )
    if job:
        return job_view(job)
    job = DocumentSync(
        project_id=project_id,
        configuration_version=row.version,
        requested_by=actor.user.id,
        device_id=actor.device.id,
        grant_id=actor.grant.id if actor.grant else None,
    )
    db.add(job)
    db.flush()
    hub.audit(db, actor.user.id, "documents.sync_requested", job.id, request_id, project_id)
    return job_view(job)


def snapshot(db, project_id, expected=None):
    row = db.get(DocumentRepository, project_id)
    if not row or not row.snapshot_id:
        raise HubError("DOCUMENT_INDEX_NOT_READY", 409)
    if expected and expected != row.snapshot_id:
        raise HubError("DOCUMENT_INDEX_VERSION_CHANGED", 409)
    return row


def reference(repo, doc):
    return {
        "path": doc.path,
        "title": doc.title,
        "sha256": doc.sha256,
        "byte_size": doc.byte_size,
        "git_commit": repo.git_commit,
        "snapshot_id": str(repo.snapshot_id),
        "repository_url": repo.repository_url,
        "content_trust": "untrusted-project-document",
    }


def search(db, project_id, body):
    repo = snapshot(db, project_id, body.snapshot_id)
    query = body.query.strip()
    statement = select(ProjectDocument).where(
        ProjectDocument.project_id == project_id, ProjectDocument.snapshot_id == repo.snapshot_id
    )
    if query:
        # Literal matching supports Chinese and treats %, _ and SQL fragments as text.
        statement = statement.where(
            or_(
                func.strpos(func.lower(ProjectDocument.content), query.lower()) > 0,
                func.strpos(func.lower(ProjectDocument.path), query.lower()) > 0,
            )
        )
    rows = list(
        db.scalars(statement.order_by(ProjectDocument.path).offset(body.offset).limit(body.limit + 1))
    )
    items = []
    for doc in rows[: body.limit]:
        position = max(0, doc.content.lower().find(query.lower()) - 100) if query else 0
        excerpt = doc.content[position : position + 600]
        items.append(
            reference(repo, doc)
            | {"excerpt": excerpt, "excerpt_start_line": doc.content[:position].count("\n") + 1}
        )
    return {
        "items": items,
        "snapshot_id": str(repo.snapshot_id),
        "git_commit": repo.git_commit,
        "has_more": len(rows) > body.limit,
        "next_offset": body.offset + len(items),
        "search_mode": "literal-substring",
    }


def read(db, project_id, body):
    repo = snapshot(db, project_id, body.snapshot_id)
    doc = db.get(ProjectDocument, (project_id, body.path))
    if not doc or doc.snapshot_id != repo.snapshot_id:
        raise HubError("DOCUMENT_NOT_FOUND", 404)
    return reference(repo, doc) | {"content": doc.content, "line_count": len(doc.content.splitlines())}


def markdown_document(path, data):
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise HubError("DOCUMENT_NOT_UTF8", 422) from exc
    if "\0" in content:
        raise HubError("DOCUMENT_BINARY_CONTENT", 422)
    heading = re.search(r"^#{1,6}\s+(.+)$", content, re.MULTILINE)
    title = heading[1].strip() if heading else path.rsplit("/", 1)[-1]
    return {
        "path": path,
        "title": title[:300],
        "content": content,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }
