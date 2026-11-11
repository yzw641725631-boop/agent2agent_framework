"""Version 2: bounded canonical manifest and raw file bodies in a gzip stream.

Upload validation retains at most one 16 MiB body. The base64 representation is
created only for explicit recovery callers or the single requested audit file.
"""
import base64
import hashlib
import json
import re
import struct

from . import archive_codec as codec
from .security import canonical, payload_hash

MIB = 1024 * 1024
MAX_MANIFEST = 8 * MIB
MAX_BASE_FILES = 10000
MAX_BASE_BYTES = 64 * MIB
MAX_BASE_FILE = 16 * MIB
MAX_CHANGES = 100
MAX_CHANGE_BYTES = 16 * MIB


def shape(value, keys):
    return isinstance(value, dict) and set(value) == set(keys.split())


def read(stream, length):
    result = stream.read(length)
    if len(result) != length:
        codec.fail()
    return result


def layout(files, changes):
    working = {f["path"].lower(): f["path"] for f in files}
    for entry in changes:
        key = codec.safe_path(entry["path"])
        if entry["status"] == "deleted":
            working.pop(key, None)
        else:
            if key in working and working[key] != entry["path"]:
                codec.fail()
            working[key] = entry["path"]
    directories = {}
    for key, path in working.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            folded = parent.lower()
            if folded in working or folded in directories and directories[folded] != parent:
                codec.fail()
            directories[folded] = parent
        if key in directories:
            codec.fail()


def manifest(stream, log):
    length = struct.unpack(">I", read(stream, 4))[0]
    if not 1 <= length <= MAX_MANIFEST:
        codec.fail("WORK_ARCHIVE_LIMIT")
    raw = read(stream, length)
    value = json.loads(raw, object_pairs_hook=codec.unique_object, parse_constant=lambda _: codec.fail())
    if not shape(value, "schema worklog_id evidence evidence_hash baseline contents"):
        codec.fail()
    if type(value["schema"]) is not int or value["schema"] != 2 or value["worklog_id"] != str(log.id):
        codec.fail()
    if raw != canonical(value).encode():
        codec.fail()
    if canonical(value["evidence"]) != canonical(log.change_evidence) or value["evidence_hash"] != log.evidence_hash:
        codec.fail("WORK_ARCHIVE_EVIDENCE_MISMATCH")
    if payload_hash(value["evidence"]) != value["evidence_hash"]:
        codec.fail("WORK_ARCHIVE_EVIDENCE_MISMATCH")
    baseline = value["baseline"]
    if not shape(baseline, "commit_base64 files"):
        codec.fail()
    commit = codec.content(baseline["commit_base64"], 65536)
    if codec.git_hash("commit", commit) != value["evidence"]["base_commit"]:
        codec.fail("WORK_ARCHIVE_BASE_MISMATCH")
    files, changes = baseline["files"], value["contents"]
    evidence = value["evidence"]["files"]
    if (not isinstance(files, list) or len(files) > MAX_BASE_FILES
        or not isinstance(changes, list) or len(changes) > MAX_CHANGES
        or not isinstance(evidence, list) or len(evidence) > MAX_CHANGES):
        codec.fail("WORK_ARCHIVE_LIMIT")
    expected, seen = {}, set()
    for entry in evidence:
        key = codec.safe_path(entry["path"])
        if key in seen:
            codec.fail()
        seen.add(key)
        if entry["status"] == "deleted":
            if entry["bytes"] != 0 or entry["sha256"] is not None or entry["mode"] != "000000":
                codec.fail()
        elif entry["status"] in {"added", "modified"} and entry["mode"] in {"100644", "100755"}:
            expected[entry["path"]] = entry
        else:
            codec.fail()
    if len(changes) != len(expected):
        codec.fail()
    for section, entries, maximum, budget in [
        ("baseline", files, MAX_BASE_FILE, MAX_BASE_BYTES),
        ("contents", changes, codec.MAX_FILE, MAX_CHANGE_BYTES),
    ]:
        seen, total = set(), 0
        for file in entries:
            if not shape(file, "path bytes sha256 mode git_oid" if section == "baseline" else "path bytes sha256"):
                codec.fail()
            key = codec.safe_path(file["path"])
            if (key in seen or type(file["bytes"]) is not int or not 0 <= file["bytes"] <= maximum
                or not isinstance(file["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", file["sha256"])):
                codec.fail()
            seen.add(key)
            total += file["bytes"]
            if section == "baseline":
                if (file["mode"] not in {"100644", "100755"} or not isinstance(file["git_oid"], str)
                    or not re.fullmatch(r"[a-f0-9]{40}", file["git_oid"])):
                    codec.fail()
            else:
                entry = expected.get(file["path"])
                if not entry or entry["bytes"] != file["bytes"] or entry["sha256"] != file["sha256"]:
                    codec.fail("WORK_ARCHIVE_EVIDENCE_MISMATCH")
        if total > budget:
            codec.fail("WORK_ARCHIVE_LIMIT")
    if not commit.startswith(("tree " + codec.tree_hash(files) + "\n").encode()):
        codec.fail("WORK_ARCHIVE_BASE_MISMATCH")
    layout(files, evidence)
    return value


def decode_stream(stream, log, *, retain=True):
    value = manifest(stream, log)
    digest = payload_hash(value)
    baseline, contents = [], []
    total, changed_bytes = 0, 0
    for section, files, destination in [
        ("baseline", value["baseline"]["files"], baseline), ("contents", value["contents"], contents)
    ]:
        for file in files:
            body = read(stream, file["bytes"])
            codec.secret(body)
            if hashlib.sha256(body).hexdigest() != file["sha256"]:
                codec.fail("WORK_ARCHIVE_HASH_MISMATCH")
            if section == "baseline" and codec.git_hash("blob", body) != file["git_oid"]:
                codec.fail("WORK_ARCHIVE_BASE_MISMATCH")
            item = {key: file[key] for key in (("path", "mode", "git_oid") if section == "baseline" else ("path",))}
            if retain is True or retain == (section, file["path"]):
                item["content_base64"] = base64.b64encode(body).decode()
            destination.append(item)
            if section == "baseline":
                total += len(body)
            else:
                changed_bytes += len(body)
            del body
    # Reading through EOF validates the gzip trailer and rejects extra members/payloads.
    if stream.read(1):
        codec.fail()
    result = {**value, "baseline": {"commit_base64": value["baseline"]["commit_base64"], "files": baseline}, "contents": contents}
    return result, digest, {
        "archive_schema": 2, "minimum_desktop_version": "0.23.0", "baseline_files": len(baseline),
        "changed_files": len(contents), "baseline_bytes": total, "changed_bytes": changed_bytes,
        "working_tree_restorable": True, "git_history_included": False,
        "base_commit": value["evidence"]["base_commit"], "head_commit": value["evidence"]["head_commit"],
    }
