"""Stable repository resources. Platform grants never imply upstream Git rights."""

import uuid

from sqlalchemy import select, text

from .documents import token_cipher
from .models import DocumentRepository, ProjectRepository
from .schemas import RepositoryPermission
from .security import HubError

CAPABILITIES = {"code:read", "code:write", "documents:read"}


def permitted(db, project_id, repository_id, capability):
    return bool(
        db.scalar(
            text("SELECT hub.has_repository_cap(:p,:r,:c)"),
            {"p": project_id, "r": repository_id, "c": capability},
        )
    )


def require(db, project_id, repository_id, capability="code:read"):
    if not permitted(db, project_id, repository_id, capability):
        raise HubError("REPOSITORY_NOT_ACCESSIBLE", 404)
    row = db.scalar(
        select(ProjectRepository).where(
            ProjectRepository.id == repository_id, ProjectRepository.project_id == project_id
        )
    )
    if not row:
        raise HubError("REPOSITORY_NOT_ACCESSIBLE", 404)
    return row


def validate_permissions(db, project_id, values):
    result, seen = [], set()
    for value in values:
        value = (
            value if isinstance(value, RepositoryPermission) else RepositoryPermission.model_validate(value)
        )
        if value.repository_id in seen:
            raise HubError("DUPLICATE_REPOSITORY_PERMISSION", 422)
        seen.add(value.repository_id)
        repo = db.scalar(
            select(ProjectRepository).where(
                ProjectRepository.id == value.repository_id, ProjectRepository.project_id == project_id
            )
        )
        if not repo or repo.status != "active":
            raise HubError("REPOSITORY_NOT_ACCESSIBLE", 404)
        result.append(value.model_dump(mode="json"))
    return sorted(result, key=lambda item: item["repository_id"])


def view(db, row, *, manager=False):
    result = {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "slug": row.slug,
        "name": row.name,
        "platform": row.platform,
        "repository_url": row.repository_url,
        "branch": row.branch,
        "status": row.status,
        "version": row.version,
        "capabilities": sorted(cap for cap in CAPABILITIES if permitted(db, row.project_id, row.id, cap)),
    }
    if manager:
        result.update(
            auth_mode=row.auth_mode, token_set=bool(row.encrypted_token), installation_id=row.installation_id
        )
    return result


def page(hub, db, actor, project_id):
    project = hub.project(db, actor, project_id)
    manager = bool(db.scalar(text("SELECT hub.has_project_cap(:p,'repositories:manage')"), {"p": project_id}))
    rows = db.scalars(
        select(ProjectRepository)
        .where(ProjectRepository.project_id == project_id)
        .order_by(ProjectRepository.slug)
        .limit(101)
    )
    rows = list(rows)
    return {
        "project_id": str(project_id),
        "repository_mode": project.repository_mode,
        "can_manage": manager,
        "items": [view(db, row, manager=manager) for row in rows[:100]],
        "has_more": len(rows) > 100,
        "upstream_credentials_exposed": False,
    }


def visibility(db, project):
    root = bool(db.scalar(text("SELECT hub.is_superadmin()")))
    rows = list(
        db.scalars(
            select(ProjectRepository).where(
                ProjectRepository.project_id == project.id, ProjectRepository.status == "active"
            )
        )
    )
    return {
        "all_repositories": root,
        "unscoped": root or project.repository_mode == "legacy",
        "code_read_ids": [str(row.id) for row in rows if permitted(db, project.id, row.id, "code:read")],
        "code_write_ids": [str(row.id) for row in rows if permitted(db, project.id, row.id, "code:write")],
        "document_read_ids": [
            str(row.id) for row in rows if permitted(db, project.id, row.id, "documents:read")
        ],
    }


def register(hub, db, actor, project_id, body, request_id):
    hub.project(db, actor, project_id, "repositories:manage")
    hub.manage_members(db, actor, project_id)
    existing = db.scalar(
        select(ProjectRepository.id).where(
            ProjectRepository.project_id == project_id,
            (ProjectRepository.slug == body.slug) | (ProjectRepository.repository_url == body.repository_url),
        )
    )
    if existing:
        raise HubError("REPOSITORY_ALREADY_REGISTERED", 409)
    count = db.scalars(select(ProjectRepository.id).where(ProjectRepository.project_id == project_id)).all()
    if len(count) >= 100:
        raise HubError("REPOSITORY_LIMIT_EXCEEDED", 422)
    row = ProjectRepository(
        id=uuid.uuid4(),
        project_id=project_id,
        **body.model_dump(exclude={"token"}, mode="python"),
        encrypted_token=token_cipher(hub.settings).encrypt(body.token.get_secret_value().encode()).decode()
        if body.token is not None
        else None,
    )
    db.add(row)
    db.flush()
    db.execute(text("SELECT hub.activate_repository_scope(:p)"), {"p": project_id})
    db.expire_all()
    hub.audit(
        db, actor.user.id, "repository.registered", row.id, request_id, project_id, repository_id=row.id
    )
    return view(db, row, manager=True)


def change_status(hub, db, actor, project_id, repository_id, body, request_id):
    # Archiving/reactivating can affect every member's access; only the superadmin may do it.
    from .service import require_root

    require_root(actor)
    hub.project(db, actor, project_id)
    row = db.scalar(
        select(ProjectRepository).where(
            ProjectRepository.id == repository_id, ProjectRepository.project_id == project_id
        )
    )
    if not row:
        raise HubError("REPOSITORY_NOT_ACCESSIBLE", 404)
    if row.version != body.expected_version:
        raise HubError("REPOSITORY_VERSION_CONFLICT", 409)
    if body.status == "archived" and row.status != "archived":
        from . import closeouts
        closeouts.create(hub, db, project_id=project_id, reason="repository_archived", repository_ids=[str(repository_id)])
    row.status, row.version = body.status, row.version + 1
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "repository.status_changed",
        row.id,
        request_id,
        project_id,
        repository_id=row.id,
        status=row.status,
    )
    return view(db, row, manager=True)


def change_credential(hub, db, actor, project_id, repository_id, body, request_id):
    from .service import require_root

    require_root(actor)
    row = require(db, project_id, repository_id)
    if row.version != body.expected_version:
        raise HubError("REPOSITORY_VERSION_CONFLICT", 409)
    if row.auth_mode != "token":
        raise HubError("REPOSITORY_CREDENTIAL_MODE_MISMATCH", 422)
    row.encrypted_token = token_cipher(hub.settings).encrypt(body.token.get_secret_value().encode()).decode()
    row.version += 1
    db.flush()
    hub.audit(
        db,
        actor.user.id,
        "repository.credential_rotated",
        row.id,
        request_id,
        project_id,
        repository_id=row.id,
    )
    return view(db, row, manager=True)


def source_access(hub, db, actor, project_id, repository_id=None, *, manage=False):
    hub.project(db, actor, project_id, "documents:manage" if manage else "documents:read")
    source_id = repository_id or project_id
    if not db.scalar(text("SELECT hub.can_read_source(:p,:s)"), {"p": project_id, "s": source_id}):
        raise HubError("DOCUMENT_SOURCE_NOT_ACCESSIBLE", 404)
    if repository_id:
        repo = require(db, project_id, repository_id, "code:read" if manage else "documents:read")
        return repo
    return None


def document_sources(db, project_id):
    rows = db.scalars(
        select(DocumentRepository)
        .where(DocumentRepository.project_id == project_id)
        .order_by(DocumentRepository.id)
    )
    return {
        "items": [
            {
                "source_id": str(row.id),
                "repository_id": str(row.repository_id) if row.repository_id else None,
                "repository_url": row.repository_url,
                "version": row.version,
                "snapshot_id": str(row.snapshot_id) if row.snapshot_id else None,
                "document_count": row.document_count,
            }
            for row in rows
        ]
    }
