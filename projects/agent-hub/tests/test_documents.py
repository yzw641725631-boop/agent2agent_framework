import io
import json
import stat
import uuid
import zipfile

import httpx
import pytest
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.orm import sessionmaker

from agent_hub.db import context, session_factory
from agent_hub.document_worker import DocumentWorker, GitHubSource, archive_documents
from agent_hub.documents import token_cipher
from agent_hub.models import DocumentRepository, DocumentSync, ProjectDocument
from agent_hub.security import HubError, now

COMMIT = "a" * 40
TOKEN = "test-only-read-token"
CONFIG = {
    "expected_version": 0,
    "repository_url": "https://github.com/example/docs.git",
    "branch": "main",
    "include_paths": ["docs", "AGENTS.md"],
    "token": TOKEN,
}


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(path if isinstance(path, zipfile.ZipInfo) else "root/" + path, content)
    return output.getvalue()


def fixture_source(cfg, files=None, *, reject=False, redirects=None):
    archive = zipped(
        files
        or [
            ("docs/架构.md", "# 系统架构\n通过 MCP 读取共享文档。\n100%_literal"),
            ("AGENTS.md", "# Agent 约定\n每次查询需鉴权。"),
            ("not-selected.md", "private outside include paths"),
        ]
    )

    def reply(request):
        assert request.url.scheme == "https"
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer " + TOKEN
            if reject:
                return httpx.Response(404, json={"message": "not returned to user"})
            if "/commits/" in request.url.path:
                return httpx.Response(200, json={"sha": COMMIT})
            assert request.url.path.endswith("/zipball/" + COMMIT)
            return httpx.Response(
                302,
                headers={
                    "location": redirects
                    or f"https://codeload.github.com/example/docs/legacy.zip/{COMMIT}?token=private"
                },
            )
        assert request.url.host == "codeload.github.com"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=archive)

    return GitHubSource(cfg, transport=httpx.MockTransport(reply))


@pytest.fixture
def indexer(database):
    cfg, owner = database
    engine = create_engine(owner.kw["bind"].url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def role(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE hub_indexer")
        connection.commit()

    factory = sessionmaker(engine, expire_on_commit=False)
    yield lambda source=None: DocumentWorker(cfg, factory=factory, source=source or fixture_source(cfg))
    engine.dispose()


def prepare(w, project):
    base = f"/api/projects/{project}/documents"
    w.ok("PUT", base + "/repository", who=w.root, body=CONFIG)
    return base


def test_document_round_trip_literal_search_scope_revocation_and_rls(world, database, indexer):
    w = world
    project, other = w.project(), w.project("other-project")
    base = prepare(w, project)
    operation = str(uuid.uuid4())
    job = w.ok("POST", base + "/syncs", who=w.root, body={}, idem=operation)
    assert w.ok("POST", base + "/syncs", who=w.root, body={}, idem=operation) == job
    assert w.ok("POST", base + "/syncs", who=w.root, body={})["id"] == job["id"]
    assert indexer().run_once()
    assert not indexer().run_once()
    repo = w.ok("GET", base + "/repository", who=w.root)
    assert repo["latest_sync"]["status"] == "succeeded"
    assert repo["git_commit"] == COMMIT and repo["document_count"] == 2
    assert TOKEN not in json.dumps(repo)
    reader = w.member(project)
    grant = w.grant(reader, project, ["project:read", "documents:read"])
    w.start(reader, grant)
    result = w.ok("POST", base + "/search", who=reader, body={"query": "共享文档"})
    assert len(result["items"]) == 1
    doc = result["items"][0]
    assert doc["path"] == "docs/架构.md" and doc["git_commit"] == COMMIT
    assert doc["content_trust"] == "untrusted-project-document"
    content = w.ok(
        "POST", base + "/read", who=reader, body={"path": doc["path"], "snapshot_id": doc["snapshot_id"]}
    )
    assert "MCP" in content["content"]
    assert len(w.ok("POST", base + "/search", who=reader, body={"query": "%_"})["items"]) == 1
    assert w.ok("POST", base + "/search", who=reader, body={"query": "' OR TRUE;--"})["items"] == []
    page = w.ok("POST", base + "/search", who=reader, body={"limit": 1})
    assert page["has_more"]
    assert (
        len(
            w.ok(
                "POST",
                base + "/search",
                who=reader,
                body={"limit": 1, "offset": page["next_offset"], "snapshot_id": page["snapshot_id"]},
            )["items"]
        )
        == 1
    )
    assert "token_set" not in w.ok("GET", base + "/repository", who=reader)
    assert w.call("PUT", base + "/repository", who=reader, body=CONFIG).status_code == 404
    assert w.call("POST", base + "/syncs", who=reader, body={}).status_code == 404
    assert w.call("POST", f"/api/projects/{other}/documents/search", who=reader, body={}).status_code == 404
    assert w.call("POST", base + "/read", who=reader, body={"path": "not-selected.md"}).status_code == 404
    runtime = session_factory(database[0])
    with runtime() as db:
        assert not list(db.scalars(select(ProjectDocument)))
        assert not list(db.scalars(select(DocumentRepository)))
        context(db, reader.user_id, reader.enrollment["device_id"], grant)
        assert len(list(db.scalars(select(ProjectDocument)))) == 2
        assert not list(db.scalars(select(DocumentSync)))
        assert not db.scalar(text("SELECT pg_has_role(current_user,'hub_indexer','member')"))
    runtime.kw["bind"].dispose()
    w.ok("POST", f"/api/projects/{project}/members/{reader.user_id}/revoke", who=w.root, body={})
    assert (
        w.call("POST", base + "/read", who=reader, body={"path": doc["path"]}).json()["code"]
        == "PROJECT_ACCESS_REVOKED"
    )


def test_document_config_conflict_credential_encryption_and_failure_preserves_snapshot(
    world, database, indexer
):
    w = world
    project = w.project()
    base = prepare(w, project)
    w.ok("POST", base + "/syncs", who=w.root, body={})
    indexer().run_once()
    original = w.ok("GET", base + "/repository", who=w.root)
    w.ok("POST", base + "/syncs", who=w.root, body={})
    indexer(fixture_source(database[0], reject=True)).run_once()
    failed = w.ok("GET", base + "/repository", who=w.root)
    assert failed["snapshot_id"] == original["snapshot_id"]
    assert failed["latest_sync"]["error_code"] == "REPOSITORY_NOT_ACCESSIBLE"
    assert w.call("PUT", base + "/repository", who=w.root, body=CONFIG).status_code == 409
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        encrypted = db.get(DocumentRepository, uuid.UUID(project)).encrypted_token
        assert (
            TOKEN not in encrypted and token_cipher(database[0]).decrypt(encrypted.encode()).decode() == TOKEN
        )
    runtime.kw["bind"].dispose()
    updated = CONFIG | {"expected_version": 1, "include_paths": ["AGENTS.md"]}
    w.ok("PUT", base + "/repository", who=w.root, body=updated)
    assert (
        w.call("POST", base + "/read", who=w.root, body={"path": "docs/架构.md"}).json()["code"]
        == "DOCUMENT_INDEX_NOT_READY"
    )
    w.ok("POST", base + "/syncs", who=w.root, body={})
    indexer().run_once()
    assert (
        w.call("POST", base + "/search", who=w.root, body={"snapshot_id": original["snapshot_id"]}).json()[
            "code"
        ]
        == "DOCUMENT_INDEX_VERSION_CHANGED"
    )
    assert w.call("POST", base + "/read", who=w.root, body={"path": "docs/架构.md"}).status_code == 404


def test_project_read_alone_cannot_read_documents_even_through_database(world, database, indexer):
    w = world
    project = w.project()
    base = prepare(w, project)
    w.ok("POST", base + "/syncs", who=w.root, body={})
    indexer().run_once()
    member = w.member(project)
    grant = w.grant(member, project)
    w.start(member, grant)
    assert w.call("POST", base + "/search", who=member, body={}).status_code == 404
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, member.user_id, member.enrollment["device_id"], grant)
        assert not list(db.scalars(select(ProjectDocument)))
        assert not list(db.scalars(select(DocumentRepository)))
    runtime.kw["bind"].dispose()


def test_inflight_index_cannot_publish_after_configuration_changes(world, database, indexer):
    w = world
    project = w.project()
    base = prepare(w, project)
    w.ok("POST", base + "/syncs", who=w.root, body={})
    original_source = fixture_source(database[0])

    class Changed:
        def load(self, repository):
            # This API request would deadlock if the worker held the identity lock during I/O.
            w.ok(
                "PUT",
                base + "/repository",
                who=w.root,
                body=CONFIG | {"expected_version": 1, "include_paths": ["new-docs"]},
            )
            return original_source.load(repository)

    indexer(Changed()).run_once()
    result = w.ok("GET", base + "/repository", who=w.root)
    assert result["snapshot_id"] is None
    assert result["latest_sync"]["status"] == "cancelled"


def test_admin_needs_approved_documents_manage_and_inflight_revoke_prevents_publication(
    world, database, indexer
):
    w = world
    project = w.project()
    base = prepare(w, project)
    admin = w.administrator(project)
    assert w.call("POST", base + "/syncs", who=admin, body={}).status_code == 404
    w.start(admin, w.grant(admin, project, ["project:read", "documents:read", "documents:manage"]))
    w.ok("POST", base + "/syncs", who=admin, body={})
    original_source = fixture_source(database[0])

    class Revoked:
        def load(self, repository):
            w.ok("POST", f"/api/projects/{project}/members/{admin.user_id}/revoke", who=w.root, body={})
            return original_source.load(repository)

    indexer(Revoked()).run_once()
    result = w.ok("GET", base + "/repository", who=w.root)
    assert result["latest_sync"]["error_code"] == "SYNC_AUTHORIZATION_CHANGED"
    assert result["snapshot_id"] is None


def test_worker_recovers_expired_lease_and_fences_old_claim(world, indexer):
    w = world
    project = w.project()
    base = prepare(w, project)
    w.ok("POST", base + "/syncs", who=w.root, body={})
    worker = indexer()
    old_source = worker.source

    class Reclaimed:
        def load(self, repository):
            with worker.sessions.begin() as db:
                db.execute(
                    update(DocumentSync)
                    .where(DocumentSync.project_id == uuid.UUID(project))
                    .values(lease_until=now())
                )
            replacement = indexer()
            assert replacement.run_once()
            return "b" * 40, [], 0

    worker.source = Reclaimed()
    worker.run_once()
    result = w.ok("GET", base + "/repository", who=w.root)
    assert result["git_commit"] == COMMIT and result["document_count"] == 2
    assert result["latest_sync"]["attempts"] == 2
    assert old_source is not None


@pytest.mark.parametrize(
    "change",
    [
        {"repository_url": "http://127.0.0.1/private"},
        {"repository_url": "https://github.com@evil.example/a/b"},
        {"repository_url": "https://github.com/a/b?token=secret"},
        {"include_paths": ["."]},
        {"include_paths": ["../secret"]},
        {"branch": "../main"},
        {"branch": "main;execute"},
    ],
)
def test_repository_configuration_rejects_unsafe_paths_and_urls(world, change):
    project = world.project()
    assert (
        world.call(
            "PUT", f"/api/projects/{project}/documents/repository", who=world.root, body=CONFIG | change
        ).status_code
        == 422
    )


def test_archive_boundaries_and_credential_redirects(database):
    link = zipfile.ZipInfo("root/docs/link.md")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    docs, skipped = archive_documents(
        zipped(
            [("docs/ok.md", "# valid"), (link, "../../secret"), ("docs/服务器总册_明文.md", "never index")]
        ),
        ["docs"],
    )
    assert [doc["path"] for doc in docs] == ["docs/ok.md"] and skipped == 2
    for files, code in [
        ([("../escape.md", "data")], "INVALID_REPOSITORY_ARCHIVE"),
        ([("docs/big.md", "x" * (256 * 1024 + 1))], "DOCUMENT_LIMIT_EXCEEDED"),
        ([("docs/binary.md", b"\xff\x00")], "DOCUMENT_NOT_UTF8"),
    ]:
        with pytest.raises(HubError) as error:
            archive_documents(zipped(files), ["docs"])
        assert error.value.code == code
    repository = CONFIG | {"encrypted_token": token_cipher(database[0]).encrypt(TOKEN.encode()).decode()}
    with pytest.raises(HubError) as error:
        fixture_source(database[0], redirects="https://127.0.0.1/private").load(repository)
    assert error.value.code == "REPOSITORY_REDIRECT_REJECTED"
