"""Durable local-server artifacts and immutable, server-verified delivery receipts."""
import asyncio
import hashlib
import hmac
import io
import os
import secrets
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy import select, text

from . import archive_codec, github_credentials, repositories, tasks
from .models import CodeDelivery, Worklog, WorkSnapshot
from .schemas import LeaseInput
from .security import HubError, canonical, now, payload_hash, rate_limit

SLOTS = threading.BoundedSemaphore(1)


class ArtifactStore:
    def __init__(self, settings):
        self.settings = settings

    def root(self):
        if not self.settings.artifact_directory:
            raise HubError("WORK_ARCHIVE_NOT_CONFIGURED", 503)
        root = Path(self.settings.artifact_directory)
        if not root.is_absolute():
            raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503)
        for directory in [root, *root.parents]:
            if directory.is_symlink():
                raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503) from exc
        if not root.is_dir():
            raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503)
        return root

    def key(self, snapshot_id):
        return hmac.digest(self.settings.secret.get_secret_value().encode(),
                           b"agent-hub-work-archive-v1\0" + str(snapshot_id).encode(), "sha256")

    def aad(self, row):
        return canonical({key: str(getattr(row, key)) for key in (
            "id", "project_id", "task_id", "worklog_id", "evidence_hash", "manifest_hash", "archive_hash")}).encode()

    def file(self, snapshot_id):
        return self.root() / (str(uuid.UUID(str(snapshot_id))) + ".bin")

    def write(self, row, data):
        root = self.root()
        try:
            if shutil.disk_usage(root).free < len(data) * 3 + 256 * 1024 * 1024:
                raise HubError("WORK_ARCHIVE_STORAGE_FULL", 507)
        except OSError as exc:
            raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503) from exc
        nonce = secrets.token_bytes(12)
        cipher = Cipher(algorithms.AES(self.key(row.id)), modes.GCM(nonce)).encryptor()
        cipher.authenticate_additional_data(self.aad(row))
        target = self.file(row.id)
        temporary = root / ("upload-" + str(uuid.uuid4()) + ".tmp")
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                output.write(b"HUBW1" + nonce)
                # Keep the on-disk AES-GCM format while avoiding full ciphertext copies.
                for offset in range(0, len(data), 1024 * 1024):
                    output.write(cipher.update(memoryview(data)[offset:offset + 1024 * 1024]))
                output.write(cipher.finalize())
                output.write(cipher.tag)
                output.flush()
                os.fsync(output.fileno())
            # Exclusive publication cannot overwrite an existing archive, including on POSIX.
            os.link(temporary, target)
            if os.name != "nt":
                handle = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
        except OSError as exc:
            raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return len(data) + 33

    def read(self, row):
        try:
            file = self.file(row.id)
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > archive_codec.MAX_ARCHIVE + 64:
                raise ValueError()
            with file.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != row.storage_bytes or row.storage_bytes < 33:
                    raise ValueError()
                header = stream.read(17)
                if header[:5] != b"HUBW1":
                    raise ValueError()
                stream.seek(-16, os.SEEK_END)
                cipher = Cipher(algorithms.AES(self.key(row.id)), modes.GCM(header[5:], stream.read(16))).decryptor()
                cipher.authenticate_additional_data(self.aad(row))
                stream.seek(17)
                remaining, digest = row.storage_bytes - 33, hashlib.sha256()
                with io.BytesIO() as output:
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError()
                        body = cipher.update(chunk)
                        digest.update(body)
                        output.write(body)
                        remaining -= len(chunk)
                    output.write(cipher.finalize())  # No plaintext is returned before authentication.
                    if digest.hexdigest() != row.archive_hash:
                        raise ValueError()
                    return output.getvalue()
        except HubError:
            raise
        except Exception as exc:
            raise HubError("WORK_ARCHIVE_INTEGRITY_FAILED", 503) from exc


def snapshot_view(row):
    return {"id": str(row.id), "project_id": str(row.project_id), "task_id": str(row.task_id),
        "worklog_id": str(row.worklog_id), "evidence_hash": row.evidence_hash,
        "manifest_hash": row.manifest_hash, "archive_sha256": row.archive_hash,
        "storage_bytes": row.storage_bytes, "receipt_hash": row.receipt_hash,
        "created_at": row.created_at.isoformat(), "server_code_backup": True,
        "storage": "encrypted-server-volume-and-database", "disaster_redundant": False,
        **row.metadata_value}


def delivery_view(row):
    return {"id": str(row.id), "worklog_id": str(row.worklog_id), "repository_id": str(row.repository_id),
        "git_commit": row.git_commit, "ref": row.git_ref, "receipt_hash": row.receipt_hash,
        "created_at": row.created_at.isoformat(), "verified_by": "server-github-reference-query",
        "merged": False, "deployed": False}


def get_log(hub, db, identity, project_id, task_id, log_id, *, upload=False, evidence_required=True):
    hub.project(db, identity, project_id, "tasks:write" if upload else "project:read")
    task = tasks.get_task(db, project_id, task_id)
    row = db.scalar(select(Worklog).where(Worklog.id == log_id,
        Worklog.project_id == project_id, Worklog.task_id == task_id))
    if not row:
        raise HubError("WORKLOG_NOT_ACCESSIBLE", 404)
    if upload and (row.author_id != identity.user.id or row.device_id != identity.device.id):
        raise HubError("WORK_ARCHIVE_NOT_OWNED", 403)
    if evidence_required and (not row.change_evidence or not row.evidence_hash):
        raise HubError("WORK_CHANGE_EVIDENCE_REQUIRED", 409)
    if task.repository_id:
        repositories.require(db, project_id, task.repository_id, "code:write" if upload else "code:read")
    return row


def save(hub, db, identity, log, data, *, closeout=False):
    _, manifest_hash, metadata = archive_codec.decode(data, log, retain=False)
    existing = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == log.id))
    if existing:
        if existing.manifest_hash != manifest_hash:
            raise HubError("WORK_ARCHIVE_ALREADY_RECORDED", 409)
        # A receipt is not reissued over a missing or damaged artifact.
        ArtifactStore(hub.settings).read(existing)
        return snapshot_view(existing)
    storage_bytes = len(data) + 33
    usage = db.execute(text("SELECT * FROM hub.work_archive_usage(:p)"), {"p": log.project_id}).one()
    if (usage.project_bytes + storage_bytes > hub.settings.artifact_project_bytes
        or usage.total_bytes + storage_bytes > hub.settings.artifact_total_bytes):
        raise HubError("WORK_ARCHIVE_QUOTA_EXCEEDED", 507)
    row = WorkSnapshot(id=uuid.uuid4(), created_at=now(), project_id=log.project_id,
        task_id=log.task_id, run_id=log.run_id, worklog_id=log.id, author_id=identity.user.id,
        device_id=identity.device.id, evidence_hash=log.evidence_hash, manifest_hash=manifest_hash,
        archive_hash=hashlib.sha256(data).hexdigest(), storage_bytes=storage_bytes, metadata_value=metadata)
    row.receipt_hash = payload_hash({**snapshot_view(row), "receipt_hash": None})
    actual = ArtifactStore(hub.settings).write(row, data)
    if actual != storage_bytes:
        raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503)
    db.add(row)
    db.flush()
    if not closeout:
        task = tasks.get_task(db, log.project_id, log.task_id)
        tasks.emit(hub, db, identity, task, "work.snapshot_saved", "archive:" + str(row.id),
                   run_id=log.run_id, worklog_id=log.id)
    else:
        hub.audit(db, identity.user.id, "closeout.snapshot_saved", row.id, "closeout-upload", log.project_id,
                  closeout_id=str(log.closeout_id), worklog_id=str(log.id))
    return snapshot_view(row)


def delivery_context(hub, db, identity, project_id, task_id, body):
    hub.project(db, identity, project_id, "tasks:write")
    task, run = tasks.live_run(db, identity, project_id, task_id, body)
    repo = repositories.require(db, project_id, task.repository_id, "code:write")
    log = get_log(hub, db, identity, project_id, task_id, body.worklog_id, upload=True, evidence_required=False)
    from .git_gateway import branch_for, recorded_checkpoint
    latest = recorded_checkpoint(db, identity, task, run.id, log.git_commit)
    if log.run_id != run.id or not log.git_commit or latest.id != log.id:
        raise HubError("GIT_RECORDED_CHECKPOINT_REQUIRED", 409)
    return task, run, repo, log, branch_for(task_id, run.id)


def verify_delivery(hub, db, identity, project_id, task_id, body, request_id, token_value, transport=None):
    task, run, repo, log, ref = delivery_context(hub, db, identity, project_id, task_id, body)
    material = github_credentials.credential_material(hub, db, repo)
    expected = (repo.id, repo.version, repo.repository_url, log.git_commit)
    thumb = identity.device.thumbprint
    owner, name = repo.repository_url.removeprefix("https://github.com/").split("/")
    db.commit()  # GitHub latency must never prevent revocation or hold idle transactions open.
    secret = github_credentials.exchange_credential(material, transport=transport)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    try:
        with httpx.Client(transport=transport, timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", f"https://api.github.com/repos/{owner}/{name}/git/ref/{ref[5:]}", headers=headers) as result:
                if result.status_code != 200:
                    raise HubError("GIT_REMOTE_VERIFICATION_FAILED", 502)
                data = bytearray()
                for chunk in result.iter_bytes(16384):
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise HubError("GIT_REMOTE_VERIFICATION_FAILED", 502)
                import json
                value = json.loads(data)
        if value.get("ref") != ref or value.get("object", {}).get("type") != "commit" or value["object"].get("sha") != expected[3]:
            raise HubError("GIT_REMOTE_VERIFICATION_FAILED", 409)
    except HubError:
        raise
    except Exception as exc:
        raise HubError("GIT_REMOTE_VERIFICATION_FAILED", 502) from exc
    hub.lock_session(db, token_value, thumb, project_ids=[project_id], exclusive=True)
    identity = hub.authenticate(db, token_value, thumb)
    task, run, repo, log, ref = delivery_context(hub, db, identity, project_id, task_id, body)
    if (repo.id, repo.version, repo.repository_url, log.git_commit) != expected:
        raise HubError("GIT_REMOTE_VERIFICATION_FAILED", 409)
    previous = db.scalar(select(CodeDelivery).where(CodeDelivery.worklog_id == log.id,
                                                    CodeDelivery.git_commit == log.git_commit))
    from . import code_audit

    if previous:
        return {**delivery_view(previous), "code_audit": code_audit.status(db, task)}
    row = CodeDelivery(id=uuid.uuid4(), created_at=now(), project_id=project_id, repository_id=repo.id,
        task_id=task_id, run_id=run.id, worklog_id=log.id, author_id=identity.user.id,
        device_id=identity.device.id, git_commit=log.git_commit, git_ref=ref)
    row.receipt_hash = payload_hash({**delivery_view(row), "receipt_hash": None})
    db.add(row)
    db.flush()
    tasks.emit(hub, db, identity, task, "code.delivery_verified", request_id, run_id=run.id, worklog_id=log.id)
    return {**delivery_view(row), "code_audit": code_audit.status(db, task)}


def install(app, hub, actor, token, db_dependency):
    database = Depends(db_dependency, scope="function")

    def download_slot():
        if not SLOTS.acquire(blocking=False):
            raise HubError("SERVICE_BUSY", 503)
        try:
            yield
        finally:
            SLOTS.release()

    download_guard = Depends(download_slot, scope="request")

    @app.post("/api/projects/{project_id}/tasks/{task_id}/worklogs/{log_id}/snapshot")
    async def upload(project_id: uuid.UUID, task_id: uuid.UUID, log_id: uuid.UUID, request: Request, db=database):
        identity = actor(request, db)
        get_log(hub, db, identity, project_id, task_id, log_id, upload=True)
        if request.headers.get("content-type", "").split(";")[0] != "application/gzip":
            raise HubError("WORK_ARCHIVE_INVALID", 415)
        rate_limit(app.state.security_sessions, hub.secret, f"archive:{identity.user.id}", 30, 60)
        ArtifactStore(hub.settings).root()
        value, thumb = token(request), identity.device.thumbprint
        db.commit()  # Never hold identity/project locks while waiting for an upload.
        if not SLOTS.acquire(blocking=False):
            raise HubError("SERVICE_BUSY", 503)
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
                size = 0
                try:
                    async with asyncio.timeout(300):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > archive_codec.MAX_ARCHIVE:
                                raise HubError("WORK_ARCHIVE_LIMIT", 413)
                            spool.write(chunk)
                except TimeoutError as exc:
                    raise HubError("WORK_ARCHIVE_UPLOAD_TIMEOUT", 408) from exc
                spool.seek(0)
                data = spool.read()
            if hashlib.sha256(data).hexdigest() != request.headers.get("X-Content-SHA256"):
                raise HubError("WORK_ARCHIVE_HASH_MISMATCH", 422)
            hub.lock_session(db, value, thumb, project_ids=[project_id], resources=["work-archives"], exclusive=True)
            db.expire_all()
            identity = hub.authenticate(db, value, thumb)
            log = get_log(hub, db, identity, project_id, task_id, log_id, upload=True)
            return await asyncio.to_thread(save, hub, db, identity, log, data)
        finally:
            SLOTS.release()

    @app.get("/api/projects/{project_id}/tasks/{task_id}/worklogs/{log_id}/snapshot")
    def metadata(project_id: uuid.UUID, task_id: uuid.UUID, log_id: uuid.UUID, request: Request, db=database):
        get_log(hub, db, actor(request, db), project_id, task_id, log_id)
        row = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == log_id))
        if not row:
            raise HubError("WORK_ARCHIVE_NOT_FOUND", 404)
        return snapshot_view(row)

    @app.get("/api/projects/{project_id}/tasks/{task_id}/worklogs/{log_id}/snapshot/content")
    def download(project_id: uuid.UUID, task_id: uuid.UUID, log_id: uuid.UUID, request: Request,
                 db=database, _slot=download_guard):
        get_log(hub, db, actor(request, db), project_id, task_id, log_id)
        row = db.scalar(select(WorkSnapshot).where(WorkSnapshot.worklog_id == log_id))
        if not row:
            raise HubError("WORK_ARCHIVE_NOT_FOUND", 404)
        data = ArtifactStore(hub.settings).read(row)
        hub.audit(db, request.state.actor_id, "work.snapshot_downloaded", row.id,
                  request.state.request_id, project_id, worklog_id=str(log_id))
        return Response(data, media_type="application/gzip", headers={
            "X-Content-SHA256": row.archive_hash, "Content-Disposition": 'attachment; filename="work-checkpoint.json.gz"'})

    @app.post("/api/projects/{project_id}/tasks/{task_id}/deliveries")
    def confirm_delivery(project_id: uuid.UUID, task_id: uuid.UUID, body: DeliveryInput, request: Request, db=database):
        return verify_delivery(hub, db, actor(request, db), project_id, task_id, body,
                               request.state.request_id, token(request), getattr(app.state, "git_transport", None))


class DeliveryInput(LeaseInput):
    worklog_id: uuid.UUID
