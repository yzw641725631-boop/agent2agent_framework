"""Bounded GitHub adapter and durable index jobs. No Git checkout or repository code is executed."""

import io
import logging
import re
import stat
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import delete, or_, select, text

from .db import context, coordinate, session_factory
from .documents import markdown_document, token_cipher
from .models import Audit, DocumentRepository, DocumentSync, Project, ProjectDocument, ProjectRepository
from .schemas import document_path
from .security import HubError, now

log = logging.getLogger(__name__)
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_DOCUMENT = 256 * 1024
MAX_TOTAL = 8 * 1024 * 1024


def archive_documents(data, include_paths):
    documents, skipped, total, seen = [], 0, 0, set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 50000:
                raise HubError("REPOSITORY_TOO_LARGE", 422)
            roots = {entry.filename.split("/", 1)[0] for entry in entries}
            if len(roots) != 1:
                raise HubError("INVALID_REPOSITORY_ARCHIVE", 422)
            for entry in entries:
                if entry.is_dir():
                    continue
                _, separator, path = entry.filename.partition("/")
                if not separator:
                    raise HubError("INVALID_REPOSITORY_ARCHIVE", 422)
                document_path(path)
                if path in seen:
                    raise HubError("INVALID_REPOSITORY_ARCHIVE", 422)
                seen.add(path)
                if not any(path == prefix or path.startswith(prefix + "/") for prefix in include_paths):
                    continue
                if PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}:
                    continue
                # Links/submodules/known credential locations never become readable documents.
                mode = entry.external_attr >> 16
                parts = PurePosixPath(path).parts
                if stat.S_ISLNK(mode) or any(
                    p.lower() in {".git", ".ssh", ".env"} or "明文" in p for p in parts
                ):
                    skipped += 1
                    continue
                if entry.file_size > MAX_DOCUMENT or len(documents) >= 1000:
                    raise HubError("DOCUMENT_LIMIT_EXCEEDED", 422)
                with archive.open(entry) as file:
                    blob = file.read(MAX_DOCUMENT + 1)
                total += len(blob)
                if len(blob) > MAX_DOCUMENT or total > MAX_TOTAL:
                    raise HubError("DOCUMENT_LIMIT_EXCEEDED", 422)
                documents.append({**markdown_document(path, blob), "original_bytes": blob})
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise HubError("INVALID_REPOSITORY_ARCHIVE", 422) from exc
    return documents, skipped


class GitHubSource:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def load(self, repository):
        slug = repository["repository_url"].removeprefix("https://github.com/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "AgentHub-DocumentIndexer/0.3.0",
        }
        if repository.get("credential"):
            headers["Authorization"] = "Bearer " + repository["credential"]
        elif repository["encrypted_token"]:
            credential = token_cipher(self.settings).decrypt(repository["encrypted_token"].encode()).decode()
            headers["Authorization"] = "Bearer " + credential
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                commit_url = (
                    f"https://api.github.com/repos/{slug}/commits/{quote(repository['branch'], safe='')}"
                )
                raw = self.download(client, commit_url, headers, 2 * 1024 * 1024, started)
                import json

                commit = json.loads(raw)["sha"]
                if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
                    raise HubError("INVALID_REPOSITORY_RESPONSE", 502)
                archive = self.download(
                    client,
                    f"https://api.github.com/repos/{slug}/zipball/{commit}",
                    headers,
                    MAX_ARCHIVE,
                    started,
                    redirect=True,
                )
            documents, skipped = archive_documents(archive, repository["include_paths"])
            return commit, documents, skipped
        except HubError:
            raise
        except httpx.HTTPError as exc:
            raise HubError("REPOSITORY_CONNECTION_FAILED", 502) from exc
        except Exception as exc:
            # Do not expose signed archive URLs, authorization headers or provider response bodies.
            raise HubError("INVALID_REPOSITORY_RESPONSE", 502) from exc

    @staticmethod
    def download(client, url, headers, maximum, started, *, redirect=False):
        for _ in range(4):
            with client.stream("GET", url, headers=headers) as response:
                if time.monotonic() - started > 120:
                    raise HubError("REPOSITORY_CONNECTION_FAILED", 502)
                if redirect and response.status_code in {301, 302, 303, 307, 308}:
                    next_url = response.headers.get("location", "")
                    parsed = urlsplit(next_url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "codeload.github.com"
                        or parsed.port not in {None, 443}
                        or parsed.username
                        or parsed.password
                    ):
                        raise HubError("REPOSITORY_REDIRECT_REJECTED", 502)
                    url, headers = next_url, {"User-Agent": "AgentHub-DocumentIndexer/0.3.0"}
                    continue
                if response.status_code in {401, 404}:
                    raise HubError("REPOSITORY_NOT_ACCESSIBLE", 422)
                if response.status_code in {403, 429}:
                    raise HubError("REPOSITORY_RATE_LIMITED_OR_FORBIDDEN", 422)
                if response.status_code != 200:
                    raise HubError("REPOSITORY_FETCH_FAILED", 502)
                chunks, size = [], 0
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > maximum:
                        raise HubError("REPOSITORY_TOO_LARGE", 422)
                    if time.monotonic() - started > 120:
                        raise HubError("REPOSITORY_CONNECTION_FAILED", 502)
                    chunks.append(chunk)
                return b"".join(chunks)
        raise HubError("REPOSITORY_REDIRECT_REJECTED", 502)


class DocumentWorker:
    def __init__(self, settings, *, factory=None, source=None):
        self.settings = settings
        self.sessions = factory or session_factory(settings)
        self.source = source or GitHubSource(settings)

    @staticmethod
    def authorized(db, job):
        context(db, job.requested_by, job.device_id, job.grant_id)
        allowed = db.scalar(text("SELECT hub.can_manage_documents(:p)"), {"p": job.project_id})
        allowed = allowed and db.scalar(text("SELECT hub.can_read_source(:p,:s)"), {"p": job.project_id, "s": job.source_id})
        project = db.get(Project, job.project_id)
        return allowed and project is not None and project.status == "active"

    def run_once(self):
        # Only claim/publish transactions take the identity lock; remote I/O happens between them.
        with self.sessions.begin() as db:
            eligible = or_(
                DocumentSync.status == "queued",
                (DocumentSync.status == "running") & (DocumentSync.lease_until < now()),
            )
            candidate = db.execute(
                select(DocumentSync.id, DocumentSync.requested_by, DocumentSync.project_id)
                .where(eligible)
                .order_by(DocumentSync.created_at)
                .limit(1)
            ).first()
            if candidate is None:
                return False
            coordinate(db, user_ids=[candidate[1]], project_ids=[candidate[2]])
            job = db.scalar(
                select(DocumentSync)
                .where(DocumentSync.id == candidate[0], eligible)
                .order_by(DocumentSync.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return True
            repo = db.get(DocumentRepository, job.source_id)
            if not repo or repo.version != job.configuration_version or not self.authorized(db, job):
                job.status, job.error_code, job.finished_at = "cancelled", "SYNC_AUTHORIZATION_CHANGED", now()
                return True
            if job.attempts >= 3:
                job.status, job.error_code, job.finished_at = "failed", "SYNC_WORKER_INTERRUPTED", now()
                return True
            job.status, job.attempts = "running", job.attempts + 1
            job.lease_until, job.lease_id = now() + timedelta(minutes=5), uuid.uuid4()
            job_id, lease_id, project_id, source_id = job.id, job.lease_id, job.project_id, job.source_id
            requested_by = job.requested_by
            configuration = {
                key: getattr(repo, key)
                for key in ("repository_url", "branch", "encrypted_token", "include_paths", "version")
            }
            registered = db.get(ProjectRepository, repo.repository_id) if repo.repository_id else None
            repository_version = registered.version if registered else None
            if registered:
                from .github_credentials import credential
                from .service import Hub
                try:
                    configuration["credential"] = credential(Hub(self.settings, None), db, registered,
                                                               transport=getattr(self.source, "transport", None))
                except HubError as exc:
                    job.status, job.error_code, job.finished_at = "failed", exc.code, now()
                    return True
        failure, loaded = None, None
        try:
            loaded = self.source.load(configuration)
        except HubError as exc:
            failure = exc.code
        except Exception:
            failure = "DOCUMENT_SYNC_FAILED"
        with self.sessions.begin() as db:
            coordinate(db, user_ids=[requested_by], project_ids=[project_id])
            job = db.get(DocumentSync, job_id, with_for_update=True)
            repo = db.get(DocumentRepository, source_id, with_for_update=True)
            registered = db.get(ProjectRepository, repo.repository_id) if repo and repo.repository_id else None
            if job.status != "running" or job.lease_id != lease_id:
                return True
            if (not repo or repo.version != configuration["version"] or not self.authorized(db, job)
                    or registered and registered.version != repository_version):
                job.status, job.error_code = "cancelled", "SYNC_AUTHORIZATION_CHANGED"
            elif failure:
                job.status, job.error_code = "failed", failure
            else:
                commit, documents, skipped = loaded
                from .memory import archive_document
                versions = [(doc, archive_document(db, repo, job.id, commit, doc)) for doc in documents]
                db.execute(delete(ProjectDocument).where(ProjectDocument.source_id == source_id))
                db.add_all(
                    ProjectDocument(project_id=project_id, source_id=source_id, snapshot_id=job.id,
                                    version_id=version.id, **{k: v for k, v in doc.items() if k != "original_bytes"})
                    for doc, version in versions
                )
                repo.snapshot_id, repo.git_commit, repo.indexed_at = job.id, commit, now()
                repo.document_count, repo.skipped_count = len(documents), skipped
                job.status = "succeeded"
            job.finished_at, job.lease_until = now(), None
            db.add(
                Audit(
                    actor_id=job.requested_by,
                    project_id=project_id,
                    repository_id=repo.repository_id if repo else None,
                    action="documents.sync_" + job.status,
                    object_id=str(job.id),
                    request_id="document-sync:" + str(job.id),
                    details={
                        "configuration_version": job.configuration_version,
                        "error_code": job.error_code,
                    },
                )
            )
        return True

    def serve(self):
        with self.sessions() as db:
            if not db.scalar(text("SELECT pg_has_role(current_user, 'hub_indexer', 'member')")):
                raise RuntimeError("Document worker requires the separate hub_indexer database role")
        while True:
            try:
                worked = self.run_once()
            except Exception:
                # Never print driver/network exception messages that could contain credentials.
                log.error("Document worker transaction failed; queued jobs will be retried")
                worked = False
            if not worked:
                time.sleep(2)
