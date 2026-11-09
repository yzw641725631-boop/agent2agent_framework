"""Bounded, self-contained working-tree archives; never execute or extract caller paths."""
import base64
import gzip
import hashlib
import io
import json
import re
import unicodedata

from .security import HubError, canonical, payload_hash

MIB = 1024 * 1024
MAX_ARCHIVE = 36 * MIB
MAX_JSON = 48 * MIB
MAX_BASE = 16 * MIB
MAX_FILE = 4 * MIB


def fail(code="WORK_ARCHIVE_INVALID"):
    raise HubError(code, 422)


def safe_path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 800
        or value != unicodedata.normalize("NFC", value)
        or re.search(r'[\\:"<>|?*\x00-\x1f\x7f]', value)):
        fail()
    for part in value.split("/"):
        key = part.lower()
        if (not part or part in {".", ".."} or part.endswith((".", " "))
            or re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", key)
            or key in {".git", ".ssh", ".aws", ".gnupg", ".npmrc", ".pypirc", ".netrc", ".env"}
            or key.startswith(".env.") and key != ".env.example" or "明文" in key
            or key.endswith((".pem", ".key", ".pfx", ".p12"))):
            fail("WORK_ARCHIVE_SECRET_PATH")
    return value.lower()


def content(value):
    if not isinstance(value, str) or len(value) > (MAX_FILE + 2) // 3 * 4:
        fail()
    try:
        data = base64.b64decode(value, validate=True)
    except Exception:
        fail()
    if len(data) > MAX_FILE or base64.b64encode(data).decode() != value:
        fail()
    if re.search(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})", data):
        fail("WORK_ARCHIVE_SECRET_CONTENT")
    return data


def git_hash(kind, data):
    return hashlib.sha1(kind.encode() + b" " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()


def tree_hash(files):
    root = {}
    for entry in files:
        parts = entry["path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                fail()
        if parts[-1] in node:
            fail()
        node[parts[-1]] = (entry["mode"], entry["git_oid"])

    def visit(node):
        records = []
        for key, value in node.items():
            if isinstance(value, dict):
                records.append((key.encode() + b"/", b"40000 " + key.encode() + b"\0" + bytes.fromhex(visit(value))))
            else:
                mode, oid = value
                records.append((key.encode(), mode.encode() + b" " + key.encode() + b"\0" + bytes.fromhex(oid)))
        return git_hash("tree", b"".join(data for _, data in sorted(records)))

    return visit(root)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail()
        result[key] = value
    return result


def decode(archive, log):
    if len(archive) > MAX_ARCHIVE:
        fail("WORK_ARCHIVE_LIMIT")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive)) as stream:
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            fail("WORK_ARCHIVE_LIMIT")
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda _: fail())
        if not isinstance(value, dict) or set(value) != {"schema", "worklog_id", "evidence", "evidence_hash", "baseline", "contents"}:
            fail()
        if type(value["schema"]) is not int or value["schema"] != 1 or value["worklog_id"] != str(log.id):
            fail()
        if canonical(value["evidence"]) != canonical(log.change_evidence) or value["evidence_hash"] != log.evidence_hash:
            fail("WORK_ARCHIVE_EVIDENCE_MISMATCH")
        baseline = value["baseline"]
        if not isinstance(baseline, dict) or set(baseline) != {"commit_base64", "files"}:
            fail()
        commit = content(baseline["commit_base64"])
        if len(commit) > 65536 or git_hash("commit", commit) != value["evidence"]["base_commit"]:
            fail("WORK_ARCHIVE_BASE_MISMATCH")
        files = baseline["files"]
        if not isinstance(files, list) or len(files) > 1000:
            fail("WORK_ARCHIVE_LIMIT")
        seen, total = set(), 0
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "mode", "git_oid", "content_base64"}:
                fail()
            key = safe_path(file["path"])
            if key in seen or file["mode"] not in {"100644", "100755"}:
                fail()
            seen.add(key)
            data = content(file["content_base64"])
            total += len(data)
            if total > MAX_BASE or git_hash("blob", data) != file["git_oid"]:
                fail("WORK_ARCHIVE_BASE_MISMATCH")
        if not commit.startswith(("tree " + tree_hash(files) + "\n").encode()):
            fail("WORK_ARCHIVE_BASE_MISMATCH")
        changes = value["contents"]
        expected = {f["path"]: f for f in value["evidence"]["files"] if f["status"] != "deleted"}
        if not isinstance(changes, list) or len(changes) != len(expected):
            fail()
        seen = set()
        for file in changes:
            if not isinstance(file, dict) or set(file) != {"path", "content_base64"}:
                fail()
            key = safe_path(file["path"])
            if key in seen or file["path"] not in expected:
                fail()
            seen.add(key)
            data, entry = content(file["content_base64"]), expected[file["path"]]
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                fail("WORK_ARCHIVE_EVIDENCE_MISMATCH")
        # Include deletion names in collision/path checks even though they have no payload.
        for entry in value["evidence"]["files"]:
            safe_path(entry["path"])
        working = {entry["path"].lower(): entry["path"] for entry in files}
        for entry in value["evidence"]["files"]:
            key = entry["path"].lower()
            if entry["status"] == "deleted":
                working.pop(key, None)
            else:
                if key in working and working[key] != entry["path"]:
                    fail()
                working[key] = entry["path"]
        directories = {}
        for key, path in working.items():
            parts = path.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                folded = parent.lower()
                if folded in working or folded in directories and directories[folded] != parent:
                    fail()
                directories[folded] = parent
            if key in directories:
                fail()
        return value, payload_hash(value), {"baseline_files": len(files), "changed_files": len(expected),
            "baseline_bytes": total, "working_tree_restorable": True, "git_history_included": False,
            "base_commit": value["evidence"]["base_commit"], "head_commit": value["evidence"]["head_commit"]}
    except HubError:
        raise
    except Exception as exc:
        raise HubError("WORK_ARCHIVE_INVALID", 422) from exc
