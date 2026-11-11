import base64
import copy
import gzip
import hashlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_repositories import grant, register, task
from test_tasks import claim, lease
from test_work_policy import observed_body, prepare

from agent_hub import archive_codec as codec
from agent_hub.archive_maintenance import audit
from agent_hub.db import context, session_factory
from agent_hub.models import WorkSnapshot
from agent_hub.security import HubError, canonical, payload_hash
from agent_hub.work_archives import ArtifactStore


def archive_fixture():
    original, changed = b"original\n", b"uncommitted change\n"
    files = [{"path": "src/login.ts", "mode": "100644", "git_oid": codec.git_hash("blob", original),
              "content_base64": base64.b64encode(original).decode()}]
    commit = ("tree " + codec.tree_hash(files) + "\nauthor Test <test@example.org> 1 +0000\n"
              "committer Test <test@example.org> 1 +0000\n\nbaseline\n").encode()
    sha = codec.git_hash("commit", commit)
    evidence = {"schema_version": 1, "source": "desktop-git-observer", "base_commit": sha,
        "head_commit": sha, "files": [{"path": "src/login.ts", "status": "modified",
        "sha256": hashlib.sha256(changed).hexdigest(), "bytes": len(changed), "mode": "100644"}]}
    return {"schema": 1, "worklog_id": str(uuid.uuid4()), "evidence": evidence,
        "evidence_hash": payload_hash(evidence), "baseline": {"commit_base64": base64.b64encode(commit).decode(), "files": files},
        "contents": [{"path": "src/login.ts", "content_base64": base64.b64encode(changed).decode()}]}


def compress(value):
    return gzip.compress(canonical(value).encode(), mtime=0)


def fake_log(value):
    return SimpleNamespace(id=value["worklog_id"], change_evidence=value["evidence"], evidence_hash=value["evidence_hash"])


def upload(w, path, data, who=None, sha=None):
    identity = who or w.root
    return w.client.post(path, content=data, headers={"Content-Type": "application/gzip",
        "Authorization": "DPoP " + identity.token, "DPoP": identity.key.proof("POST", path, identity.token),
        "X-Content-SHA256": sha or hashlib.sha256(data).hexdigest(), "Idempotency-Key": str(uuid.uuid4())})


def setup_archive(w, *, member=False, value_factory=archive_fixture):
    p = w.project()
    repo = register(w, p)
    who = w.root
    if member:
        who = w.member(p)
        w.start(who, grant(w, who, p, [(repo, ["code:read", "code:write"])]))
    t = task(w, p, repo, execution_mode="observed")
    run = claim(w, p, t, who)
    prepared = prepare(w, p, run, who)
    value = value_factory()
    body = observed_body(run, prepared)
    body.update(change_evidence=value["evidence"], git_commit=value["evidence"]["head_commit"],
                changed_paths=[entry["path"] for entry in value["evidence"]["files"]])
    log = w.ok("POST", f"/api/projects/{p}/tasks/{t['id']}/worklogs", who=who, body=body)
    value["worklog_id"] = log["id"]
    path = f"/api/projects/{p}/tasks/{t['id']}/worklogs/{log['id']}/snapshot"
    return p, repo, t, run, who, log, value, path


def test_self_contained_archive_authenticates_the_git_baseline_and_uncommitted_bytes():
    value = archive_fixture()
    result, manifest, metadata = codec.decode(compress(value), fake_log(value))
    assert result == value and manifest == payload_hash(value)
    assert metadata["working_tree_restorable"] and not metadata["git_history_included"]
    for section, key in [("baseline", "files"), (None, "contents")]:
        bad = copy.deepcopy(value)
        (bad[section] if section else bad)[key][0]["content_base64"] = base64.b64encode(b"tampered").decode()
        with pytest.raises(HubError):
            codec.decode(compress(bad), fake_log(value))
    bad = copy.deepcopy(value)
    bad["baseline"]["files"] = []
    with pytest.raises(HubError):
        codec.decode(compress(bad), fake_log(value))


@pytest.mark.parametrize("path", ["../escape", "C:/escape", "src/.git/config", "src/.env", "CON.txt", "a./b", "x/secret.pem", "x/明文.txt"])
def test_archive_rejects_unsafe_or_credential_paths(path):
    with pytest.raises(HubError):
        codec.safe_path(path)


def test_archive_rejects_duplicate_json_compression_bombs_and_changed_evidence(monkeypatch):
    value = archive_fixture()
    with pytest.raises(HubError):
        codec.decode(gzip.compress(b'{"schema":1,"schema":1}'), fake_log(value))
    monkeypatch.setattr(codec, "MAX_JSON", 1024)
    with pytest.raises(HubError) as error:
        codec.decode(gzip.compress(b" " * 1025), fake_log(value))
    assert error.value.code == "WORK_ARCHIVE_LIMIT"


def test_server_archive_is_encrypted_immutable_idempotent_and_required_for_submit(world, database):
    w = world
    p, repo, t, run, who, log, value, path = setup_archive(w, member=True)
    submission = {**lease(run), "worklog_id": log["id"], "evidence_hash": log["evidence_hash"], "expected_version": run["task"]["version"]}
    assert w.call("POST", f"/api/projects/{p}/tasks/{t['id']}/submit", who=who, body=submission).json()["code"] == "WORK_REMOTE_BACKUP_REQUIRED"
    data = compress(value)
    response = upload(w, path, data, who)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["server_code_backup"] and receipt["working_tree_restorable"]
    assert not receipt["git_history_included"] and not receipt["disaster_redundant"]
    assert upload(w, path, data, who).json() == receipt
    read = w.ok("GET", path.removesuffix("/snapshot"), who=who)
    assert read["code_backup"] and read["snapshot"] == receipt
    assert w.ok("GET", path, who=w.root) == receipt
    cfg = w.client.app.state.hub.settings
    with w.owner() as db:
        row = db.get(WorkSnapshot, uuid.UUID(receipt["id"]))
        store = ArtifactStore(cfg)
        assert store.read(row) == data
        on_disk = store.file(row.id).read_bytes()
        assert data not in on_disk and b"uncommitted change" not in on_disk
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        assert AESGCM(store.key(row.id)).decrypt(on_disk[5:17], on_disk[17:], store.aad(row)) == data
    assert w.ok("POST", f"/api/projects/{p}/tasks/{t['id']}/submit", who=who, body=submission)["status"] == "review"
    runtime = session_factory(database[0])
    with runtime() as db:
        context(db, who.user_id, who.enrollment["device_id"], who.tokens["grant_id"])
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE work_snapshots SET archive_hash=repeat('0',64)"))
    runtime.kw["bind"].dispose()


def test_recovery_survives_author_revocation_but_never_crosses_repository_permissions(world):
    w = world
    p, repo, t, run, author, log, value, path = setup_archive(w, member=True)
    assert upload(w, path, compress(value), author).status_code == 200
    reader = w.member(p, login="handoff-reader")
    w.start(reader, grant(w, reader, p, [(repo, ["code:read"])], caps=["project:read", "logs:read"]))
    stranger = w.member(p, login="other-repository-reader")
    pc = register(w, p, "pc")
    w.start(stranger, grant(w, stranger, p, [(pc, ["code:read"])]))
    w.ok("POST", f"/api/projects/{p}/members/{author.user_id}/revoke", who=w.root, body={})
    assert w.call("GET", path, who=author).status_code in {401, 403, 404}
    assert w.call("GET", path, who=stranger).status_code == 404
    content_path = path + "/content"
    response = w.client.get(content_path, headers={"Authorization": "DPoP " + reader.token,
        "DPoP": reader.key.proof("GET", content_path, reader.token)})
    assert response.status_code == 200, response.text
    decoded, _, _ = codec.decode(response.content, fake_log(value))
    assert decoded["contents"] == value["contents"]


def test_archive_upload_failure_never_creates_a_success_receipt(world):
    w = world
    p, repo, t, run, who, log, value, path = setup_archive(w)
    assert upload(w, path, compress(value), sha="0" * 64).json()["code"] == "WORK_ARCHIVE_HASH_MISMATCH"
    bad = copy.deepcopy(value)
    bad["contents"][0]["content_base64"] = base64.b64encode(b"wrong").decode()
    assert upload(w, path, compress(bad)).status_code == 422
    cfg = w.client.app.state.hub.settings
    cfg.artifact_project_bytes = 1
    assert upload(w, path, compress(value)).json()["code"] == "WORK_ARCHIVE_QUOTA_EXCEEDED"
    assert w.call("GET", path, who=w.root).status_code == 404
    with w.owner() as db:
        assert not list(db.scalars(select(WorkSnapshot)))


def test_corrupt_archive_is_not_downloaded_or_accepted_at_submit(world):
    w = world
    p, repo, t, run, who, log, value, path = setup_archive(w)
    receipt = upload(w, path, compress(value)).json()
    with w.owner() as db:
        row = db.get(WorkSnapshot, uuid.UUID(receipt["id"]))
        file = ArtifactStore(w.client.app.state.hub.settings).file(row.id)
        data = bytearray(file.read_bytes())
        data[-1] ^= 1
        file.write_bytes(data)
    assert w.call("GET", path + "/content", who=w.root).json()["code"] == "WORK_ARCHIVE_INTEGRITY_FAILED"
    submission = {**lease(run), "worklog_id": log["id"], "evidence_hash": log["evidence_hash"], "expected_version": run["task"]["version"]}
    assert w.call("POST", f"/api/projects/{p}/tasks/{t['id']}/submit", who=who, body=submission).json()["code"] == "WORK_ARCHIVE_INTEGRITY_FAILED"


def test_git_delivery_receipt_uses_server_upstream_verification_and_is_immutable(world):
    w = world
    p, repo, t, run, who, log, value, path = setup_archive(w)
    ref = f"refs/heads/hub/{t['id']}/{run['run']['id']}"
    observed = []
    def upstream(request):
        observed.append(str(request.url))
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/example/web/git/ref/" + ref[5:]
        return httpx.Response(200, json={"ref": ref, "object": {"type": "commit", "sha": log["git_commit"]}})
    w.client.app.state.git_transport = httpx.MockTransport(upstream)
    endpoint = f"/api/projects/{p}/tasks/{t['id']}/deliveries"
    body = {**lease(run), "worklog_id": log["id"]}
    receipt = w.ok("POST", endpoint, who=w.root, body=body)
    assert receipt["git_commit"] == log["git_commit"] and receipt["verified_by"] == "server-github-reference-query"
    assert w.ok("POST", endpoint, who=w.root, body=body) == receipt and len(observed) == 2
    w.client.app.state.git_transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "ref": ref, "object": {"type": "commit", "sha": "b" * 40}}))
    assert w.call("POST", endpoint, who=w.root, body=body).json()["code"] == "GIT_REMOTE_VERIFICATION_FAILED"


def test_revocation_during_github_verification_prevents_a_delivery_receipt_without_waiting_for_network(world):
    w = world
    p, repo, t, run, author, log, value, path = setup_archive(w, member=True)
    ref = f"refs/heads/hub/{t['id']}/{run['run']['id']}"

    def upstream(request):
        w.ok("POST", f"/api/projects/{p}/members/{author.user_id}/revoke", who=w.root, body={})
        return httpx.Response(200, json={"ref": ref, "object": {"type": "commit", "sha": log["git_commit"]}})

    w.client.app.state.git_transport = httpx.MockTransport(upstream)
    endpoint = f"/api/projects/{p}/tasks/{t['id']}/deliveries"
    response = w.call("POST", endpoint, who=author, body={**lease(run), "worklog_id": log["id"]})
    assert response.status_code in {401, 403, 404}, response.text
    with w.owner() as db:
        assert db.scalar(text("SELECT count(*) FROM code_deliveries")) == 0


def test_operator_audit_detects_missing_artifacts_and_only_quarantines_old_unrecorded_files(world):
    import os
    import time

    w = world
    p, repo, t, run, who, log, value, path = setup_archive(w)
    receipt = upload(w, path, compress(value)).json()
    cfg = w.client.app.state.hub.settings
    store = ArtifactStore(cfg)
    orphan = store.file(uuid.uuid4())
    orphan.write_bytes(b"uncommitted file")
    old = time.time() - 86401
    os.utime(orphan, (old, old))
    recent = store.file(uuid.uuid4())
    recent.write_bytes(b"potential active publication")
    result = audit(cfg, w.owner)
    assert result["healthy"] and result["verified"] == 1 and result["orphans"] == 2
    assert orphan.exists()
    result = audit(cfg, w.owner, quarantine_orphans=True)
    assert result["quarantined"] == 1 and not orphan.exists() and recent.exists()
    saved = store.file(receipt["id"])
    saved.unlink()
    result = audit(cfg, w.owner)
    assert not result["healthy"] and result["failed_snapshot_ids"] == [receipt["id"]]
