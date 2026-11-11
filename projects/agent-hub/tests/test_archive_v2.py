"""Independent wire fixtures plus encrypted API handoff over real PostgreSQL."""
import base64
import copy
import gzip
import hashlib
import json
import struct

import pytest
from test_work_archives import archive_fixture, fake_log, setup_archive, upload
from test_repositories import grant

from agent_hub import archive_codec as codec
from agent_hub.security import HubError, canonical, payload_hash


def large_fixture():
    value = archive_fixture()
    files = value["baseline"]["files"]
    for index in range(1000):
        body = ("unchanged baseline %s\n" % index).encode()
        files.append({"path": f"docs/file-{index}.md", "mode": "100644", "git_oid": codec.git_hash("blob", body),
                      "content_base64": base64.b64encode(body).decode()})
    commit = ("tree " + codec.tree_hash(files) + "\nauthor Test <test@example.org> 1 +0000\n\nbase\n").encode()
    value["baseline"]["commit_base64"] = base64.b64encode(commit).decode()
    value["evidence"]["base_commit"] = value["evidence"]["head_commit"] = codec.git_hash("commit", commit)
    value["evidence_hash"] = payload_hash(value["evidence"])
    return value


def wire(value):
    manifest = copy.deepcopy(value)
    manifest["schema"] = 2
    bodies = []
    for entries in [manifest["baseline"]["files"], manifest["contents"]]:
        for file in entries:
            body = base64.b64decode(file.pop("content_base64"))
            bodies.append(body)
            file.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    return manifest, b"".join(bodies)


def pack(manifest, bodies, *, raw=None):
    raw = canonical(manifest).encode() if raw is None else raw
    return gzip.compress(b"HUBW2\n" + struct.pack(">I", len(raw)) + raw + bodies, mtime=0)


def compress_v2(value):
    return pack(*wire(value))


def test_v2_full_recovery_selective_audit_and_validation_only_share_the_manifest_hash():
    value = large_fixture()
    manifest, bodies = wire(value)
    data, log = pack(manifest, bodies), fake_log(value)
    full, digest, metadata = codec.decode(data, log)
    assert full == {**value, "schema": 2} and digest == payload_hash(manifest)
    assert metadata["baseline_files"] == 1001 and metadata["archive_schema"] == 2
    for retain in [False, ("baseline", "docs/file-999.md"), ("contents", "src/login.ts")]:
        result, same, _ = codec.decode(data, log, retain=retain)
        assert same == digest
        retained = [(section, f["path"]) for section, entries in [
            ("baseline", result["baseline"]["files"]), ("contents", result["contents"])
        ] for f in entries if "content_base64" in f]
        assert retained == ([] if retain is False else [retain])


def test_v2_rejects_corruption_limits_duplicate_keys_unbound_evidence_and_trailing_members():
    value = archive_fixture()
    manifest, bodies = wire(value)
    log = fake_log(value)
    data = pack(manifest, bodies)
    corrupt = bytearray(data)
    corrupt[-8] ^= 1
    for invalid in [bytes(corrupt), data[:-1], data[:12], pack(manifest, bodies[:-1]),
                    pack(manifest, bodies + b"extra"), data + gzip.compress(b"extra"),
                    gzip.compress(b"HUBW2\n" + struct.pack(">I", 8 * 1024 * 1024 + 1)),
                    pack(manifest, bodies, raw=canonical(manifest).replace('"schema":2', '"schema":1,"schema":2').encode())]:
        with pytest.raises(HubError):
            codec.decode(invalid, log, retain=False)
    for field, replacement in [("bytes", 16 * 1024 * 1024 + 1), ("bytes", -1), ("bytes", True),
                                ("path", "../escape"), ("sha256", "0" * 64), ("git_oid", "0" * 40)]:
        bad = copy.deepcopy(manifest)
        bad["baseline"]["files"][0][field] = replacement
        with pytest.raises(HubError):
            codec.decode(pack(bad, bodies), log)
    bad = copy.deepcopy(manifest)
    bad["evidence_hash"] = "0" * 64
    with pytest.raises(HubError):
        codec.decode(pack(bad, bodies), log)
    bad = copy.deepcopy(manifest)
    bad["baseline"]["files"] *= 10001
    with pytest.raises(HubError):
        codec.decode(pack(bad, bodies), log)


def test_large_archive_retry_revocation_and_authorized_peer_recovery(world):
    w = world
    p, repo, task, run, author, log, value, path = setup_archive(w, member=True, value_factory=large_fixture)
    data = compress_v2(value)
    assert upload(w, path, data[:-4], author).status_code == 422
    assert w.call("GET", path, who=w.root).status_code == 404
    response = upload(w, path, data, author)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["archive_schema"] == 2 and receipt["baseline_files"] == 1001
    assert upload(w, path, data, author).json() == receipt
    reader = w.member(p, login="large-handoff-reader")
    w.start(reader, grant(w, reader, p, [(repo, ["code:read"])]))
    w.ok("POST", f"/api/projects/{p}/members/{author.user_id}/revoke", who=w.root, body={})
    assert w.call("GET", path + "/content", who=author).status_code in {401, 403, 404}
    recovered = w.call("GET", path + "/content", who=reader)
    assert recovered.status_code == 200 and recovered.content == data
    decoded, manifest, _ = codec.decode(recovered.content, fake_log(value))
    assert decoded["baseline"] == value["baseline"] and manifest == receipt["manifest_hash"]


def test_new_format_revocation_closeout_keeps_upload_only_scope(world, monkeypatch):
    import test_closeouts as closeouts

    monkeypatch.setattr(closeouts, "compress", compress_v2)
    closeouts.test_revoked_identity_can_only_handoff_then_authorized_peer_can_recover(world, "member")


def test_new_format_code_audit_reads_before_and_after_without_returning_the_baseline(world, monkeypatch):
    import test_work_archives
    from test_code_audit import test_audit_read_source_preserves_archive_trust_boundary

    monkeypatch.setattr(test_work_archives, "compress", compress_v2)
    test_audit_read_source_preserves_archive_trust_boundary(world)
