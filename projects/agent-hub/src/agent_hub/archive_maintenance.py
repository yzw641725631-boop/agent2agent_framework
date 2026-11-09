"""Operator-only artifact verification; no automatic removal of a recorded code backup."""
import os
import re
import shutil
import stat
import time
import uuid

from sqlalchemy import select, text

from .db import coordinate
from .models import WorkSnapshot
from .security import HubError
from .work_archives import ArtifactStore


def audit(settings, factory, *, quarantine_orphans=False):
    with factory() as db:
        if not db.scalar(text("SELECT pg_get_userbyid(relowner)=current_user FROM pg_class "
                              "WHERE oid='public.work_snapshots'::regclass")):
            raise HubError("LOCAL_DATABASE_OWNER_REQUIRED", 403)
        rows = list(db.scalars(select(WorkSnapshot)))
    store = ArtifactStore(settings)
    root = store.root()
    result = {"schema": 1, "recorded": len(rows), "verified": 0, "storage_bytes": 0,
              "failed_snapshot_ids": [], "orphans": 0, "stale_uploads": 0,
              "quarantined": 0, "unexpected_entries": 0, "free_bytes": shutil.disk_usage(root).free,
              "total_quota_bytes": settings.artifact_total_bytes, "disaster_redundant": False}
    recorded = {str(row.id) for row in rows}
    for row in rows:
        result["storage_bytes"] += row.storage_bytes
        try:
            store.read(row)
            result["verified"] += 1
        except HubError:
            result["failed_snapshot_ids"].append(str(row.id))
    cutoff = time.time() - 86400
    for file in root.iterdir():
        info = file.lstat()
        if file.name == "quarantine" and stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            result["unexpected_entries"] += 1
            continue
        artifact = re.fullmatch(r"([a-f0-9-]{36})\.bin", file.name)
        stale = re.fullmatch(r"upload-[a-f0-9-]{36}\.tmp", file.name)
        if artifact and artifact[1] in recorded:
            continue
        if artifact:
            try:
                uuid.UUID(artifact[1])
            except ValueError:
                result["unexpected_entries"] += 1
                continue
            result["orphans"] += 1
        elif stale:
            result["stale_uploads"] += 1
        else:
            result["unexpected_entries"] += 1
            continue
        if not quarantine_orphans or info.st_mtime >= cutoff:
            continue
        # Cross-process lock and a fresh lookup protect a publication still in flight.
        with factory.begin() as db:
            coordinate(db, resources=["work-archives"])
            if artifact and db.get(WorkSnapshot, uuid.UUID(artifact[1])):
                continue
            quarantine = root / "quarantine"
            if quarantine.is_symlink():
                raise HubError("WORK_ARCHIVE_STORAGE_UNAVAILABLE", 503)
            quarantine.mkdir(exist_ok=True, mode=0o700)
            target = quarantine / (file.name + "." + str(uuid.uuid4()))
            os.rename(file, target)
            result["quarantined"] += 1
    result["healthy"] = not result["failed_snapshot_ids"] and not result["unexpected_entries"] and result["free_bytes"] >= 256 * 1024 * 1024
    return result
