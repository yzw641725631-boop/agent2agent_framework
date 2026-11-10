"""Bounded GitHub object reader. No clone, shell, repository scripts or redirects."""

import base64
import hashlib
import json
import re
import time
from urllib.parse import quote

import httpx

from .archive_codec import safe_path
from .security import HubError, canonical


class AuditSource:
    def __init__(self, *, transport=None):
        self.transport = transport

    def read(self, repository_url, base_commit, git_commit, token, *, max_bytes=24000):
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_url):
            raise HubError("CODE_AUDIT_SOURCE_REPOSITORY_INVALID", 409)
        if any(not re.fullmatch(r"[a-f0-9]{40}", str(v)) for v in (base_commit, git_commit)):
            raise HubError("CODE_AUDIT_BASE_COMMIT_REQUIRED", 409)
        started, requests = time.monotonic(), 0
        prefix = "/repos/" + repository_url.removeprefix("https://github.com/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with httpx.Client(
            transport=self.transport, trust_env=False, follow_redirects=False, headers=headers
        ) as client:

            def get(path):
                nonlocal requests
                requests += 1
                remaining = 45 - (time.monotonic() - started)
                if remaining <= 0 or requests > 85:
                    raise HubError("CODE_AUDIT_SOURCE_BUDGET", 409)
                with client.stream(
                    "GET", "https://api.github.com" + prefix + path, timeout=min(10, remaining)
                ) as response:
                    if response.status_code != 200:
                        raise HubError("CODE_AUDIT_SOURCE_UNAVAILABLE", 502)
                    data = bytearray()
                    for block in response.iter_bytes():
                        data.extend(block)
                        if len(data) > 2 * 1024 * 1024 or time.monotonic() - started > 45:
                            raise HubError("CODE_AUDIT_SOURCE_BUDGET", 409)
                    return json.loads(data)

            comparison = get(f"/compare/{base_commit}...{git_commit}")
            files = comparison.get("files")
            if (
                comparison.get("base_commit", {}).get("sha") != base_commit
                or comparison.get("merge_base_commit", {}).get("sha") != base_commit
                or comparison.get("status") not in {"ahead", "identical"}
                or not isinstance(files, list)
                or len(files) >= 300
            ):
                raise HubError("CODE_AUDIT_COMPLETE_DIFF_REQUIRED", 409)
            # Compare API files appear only on the first page. A second, exact commit
            # lookup binds the head independently of the compare API's commit paging.
            head = get(f"/git/commits/{git_commit}")
            if head.get("sha") != git_commit:
                raise HubError("CODE_AUDIT_SOURCE_COMMIT_MISMATCH", 409)
            bundle = {"base_commit": base_commit, "git_commit": git_commit, "files": [], "gaps": []}
            manifest, paths = [], set()
            for entry in files:
                name = entry.get("filename")
                safe_path(name)
                if name in paths:
                    raise HubError("CODE_AUDIT_COMPLETE_DIFF_REQUIRED", 409)
                paths.add(name)
                previous = entry.get("previous_filename", name)
                safe_path(previous)
                status = entry.get("status")
                if status not in {"added", "modified", "removed", "renamed", "changed", "copied"}:
                    raise HubError("CODE_AUDIT_COMPLETE_DIFF_REQUIRED", 409)
                item = {
                    "path": name,
                    "previous_path": previous,
                    "status": status,
                    "before": None,
                    "after": None,
                }
                receipt = {"path": name, "previous_path": previous, "status": status, "blobs": {}}
                if len(files) > 40:
                    bundle["gaps"].append(f"Source file limit exceeded: {name}")
                    manifest.append(receipt)
                    continue
                for side, ref, source_path, needed in (
                    ("before", base_commit, previous, status != "added"),
                    ("after", git_commit, name, status != "removed"),
                ):
                    if not needed:
                        continue
                    data = get("/contents/" + quote(source_path, safe="/") + "?ref=" + ref)
                    if (
                        not isinstance(data, dict)
                        or data.get("type") != "file"
                        or data.get("encoding") != "base64"
                        or data.get("path") != source_path
                    ):
                        bundle["gaps"].append(f"Unavailable regular file: {side}:{source_path}")
                        continue
                    try:
                        raw = base64.b64decode("".join(data["content"].split()), validate=True)
                        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                        if actual != data["sha"] or data.get("size") != len(raw):
                            raise HubError("CODE_AUDIT_BLOB_MISMATCH", 409)
                        receipt["blobs"][side] = actual
                        text = raw.decode("utf-8")
                        if "\0" in text or len(raw) > max_bytes:
                            raise ValueError()
                        item[side] = text
                        if len(canonical(bundle | {"files": bundle["files"] + [item]}).encode()) > max_bytes:
                            item[side] = None
                            raise ValueError()
                    except (ValueError, UnicodeError, KeyError):
                        bundle["gaps"].append(f"Binary, invalid or over-budget file: {side}:{source_path}")
                manifest.append(receipt)
                bundle["files"].append(item)
        receipt = {
            "base_commit": base_commit,
            "git_commit": git_commit,
            "manifest": manifest,
            "manifest_hash": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
            "gaps": bundle["gaps"],
            "changed_paths": sorted(paths),
            "transport": "github-api-exact-objects",
            "repository_code_executed": False,
        }
        return bundle, receipt
