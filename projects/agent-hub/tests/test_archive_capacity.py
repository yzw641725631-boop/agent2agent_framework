"""Optional full-capacity JS interoperability gate, run in a 512 MiB server container."""
import gzip
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

import pytest
from test_archive_v2 import compress_v2
from test_work_archives import fake_log, setup_archive, upload
from test_repositories import grant

from agent_hub import archive_codec as codec
from agent_hub.security import canonical


def test_real_desktop_capacity_container_through_encrypted_api_retry_and_peer_handoff(world, tmp_path):
    fixture = os.getenv("HUB_BACKUP_CAPACITY_FIXTURE")
    if not fixture:
        pytest.skip("Explicit 80 MiB real-desktop capacity fixture required")
    root = Path(fixture)
    original = json.loads((root/"log.json").read_text())
    expected = json.loads((root/"expected.json").read_text())
    client_report = json.loads((root/"report.json").read_text())
    assert client_report["protector"] == "Electron45-Windows-DPAPI"
    assert client_report["roundtrip_and_export_verified"] is True
    fixture_value = {"evidence":original["change_evidence"],"evidence_hash":original["evidence_hash"]}
    w = world
    p, repo, task, run, author, log, value, path = setup_archive(w, member=True, value_factory=lambda: fixture_value.copy())
    rebound = tmp_path/"rebound.gz"
    with gzip.open(root/"archive.gz","rb") as incoming, gzip.open(rebound,"wb",compresslevel=6) as outgoing:
        assert incoming.read(6) == b"HUBW2\n"
        manifest = json.loads(incoming.read(struct.unpack(">I",incoming.read(4))[0]))
        manifest["worklog_id"] = log["id"]
        raw = canonical(manifest).encode()
        outgoing.write(b"HUBW2\n" + struct.pack(">I",len(raw)) + raw)
        shutil.copyfileobj(incoming,outgoing,length=1024*1024)
    data = rebound.read_bytes()
    _, digest, metadata = codec.decode(data,fake_log(value),retain=False)
    assert metadata["baseline_files"] == 10000 and metadata["baseline_bytes"] == 64*1024*1024
    assert metadata["changed_bytes"] == 16*1024*1024
    response = upload(w,path,data,author)
    assert response.status_code == 200,response.text
    receipt = response.json()
    assert receipt["manifest_hash"] == digest
    assert upload(w,path,data,author).json() == receipt
    selected, _, _ = codec.decode(data,fake_log(value),retain=("baseline","src/baseline-0.bin"))
    import base64
    body = base64.b64decode(selected["baseline"]["files"][0]["content_base64"])
    assert len(body) == 16*1024*1024 and hashlib.sha256(body).hexdigest() == expected["src/baseline-0.bin"]
    del selected, body
    reader = w.member(p,login="capacity-reader")
    w.start(reader,grant(w,reader,p,[(repo,["code:read"])]))
    w.ok("POST",f"/api/projects/{p}/members/{author.user_id}/revoke",who=w.root,body={})
    assert w.call("GET",path+"/content",who=author).status_code in {401,403,404}
    downloaded = w.call("GET",path+"/content",who=reader)
    assert downloaded.status_code == 200 and downloaded.content == data
    output = os.getenv("HUB_BACKUP_CAPACITY_REPORT")
    if output:
        import resource
        Path(output).write_text(json.dumps({"passed":True,"baseline_files":10000,"baseline_bytes":64*1024*1024,
            "changed_bytes":16*1024*1024,"compressed_bytes":len(data),"desktop_archive_sha256":client_report["archive_sha256"],
            "desktop_manifest_hash":client_report["manifest_hash"],"api_retry_idempotent":True,"revoked_author_denied":True,
            "authorized_peer_recovered":True,"selective_16m_audit_verified":True,
            "peak_process_rss_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024},indent=2)+"\n")
