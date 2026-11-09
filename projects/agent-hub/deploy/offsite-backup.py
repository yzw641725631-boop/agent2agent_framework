#!/usr/bin/env python3
# ruff: noqa: UP017, UP022
"""Copy a confirmed encrypted snapshot to an independent S3-compatible repository.

Run as operator; this script never deletes snapshots, dumps plaintext, or logs credentials.
Configuration is JSON in /opt/agent-hub/backups/offsite.json, mode 0600, owned by root.
"""

import datetime
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path("/opt/agent-hub/backups")
IMAGE = "restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510"


def protected(file):
    if not file.is_absolute() or any(item.is_symlink() for item in [file, *file.parents]):
        raise ValueError("Invalid protected path")
    info = file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Protected file must be root-owned mode 0600")
    return file


def configuration(file):
    value = json.loads(protected(file).read_text())
    expected = {"repository", "region", "access_key_id", "secret_access_key", "password_file"}
    if set(value) != expected or any(
        not isinstance(item, str) or not item or "\n" in item or "\r" in item for item in value.values()
    ):
        raise ValueError("Invalid offsite configuration")
    if not value["repository"].startswith("s3:https://"):
        raise ValueError("Offsite repository requires HTTPS S3")
    url = urlsplit(value["repository"][3:])
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.port not in (None, 443)
        or not url.path.strip("/")
    ):
        raise ValueError("Invalid repository endpoint")
    if url.hostname in ("localhost", "127.0.0.1", "47.109.23.86"):
        raise ValueError("Choose an independent offsite target")
    protected(Path(value["password_file"]))
    return value


def invoke(config, args):
    # Child environment is not printed or placed in process arguments.
    environment = dict(
        os.environ,
        AWS_ACCESS_KEY_ID=config["access_key_id"],
        AWS_SECRET_ACCESS_KEY=config["secret_access_key"],
        AWS_DEFAULT_REGION=config["region"],
        RESTIC_REPOSITORY=config["repository"],
        RESTIC_PASSWORD_FILE="/run/secrets/destination-password",
        GOMEMLIMIT="320MiB",
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        "agent-hub-offsite-copy",
        "--label",
        "agent-hub-purpose=offsite-copy",
        "--read-only",
        "--memory",
        "384m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "64",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,nosuid,noexec,size=64m",
        "-v",
        str(BASE / "restic") + ":/source:ro",
        "-v",
        str(BASE / "restic-password") + ":/run/secrets/source-password:ro",
        "-v",
        config["password_file"] + ":/run/secrets/destination-password:ro",
    ]
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD_FILE",
        "GOMEMLIMIT",
    ):
        command.extend(["-e", name])
    # An operator flock protects the read-only source; restic still locks the destination
    # for copy's append operation. See the pinned restic command's source/read-lock logic.
    try:
        result = subprocess.run(
            command + [IMAGE, "--no-cache", "--no-lock", *args],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        cleanup_owned_container()
        raise RuntimeError("Offsite operation timed out") from None
    if result.returncode:
        raise RuntimeError("Offsite operation failed")
    if len(result.stdout) > 1048576:
        raise ValueError("Unexpected offsite response size")
    return result.stdout


def copy_snapshot(config, initialize=False):
    protected(BASE / "restic-password")
    if not (BASE / "restic").is_dir() or any(
        item.is_symlink() for item in [BASE / "restic", BASE, *BASE.parents]
    ):
        raise ValueError("Real encrypted source repository required")
    source = json.loads(protected(BASE / "last-success.json").read_text())
    snapshot = source.get("snapshot_id", "")
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot) or source.get("full_repository_check") is not True:
        raise ValueError("A confirmed local snapshot is required")
    timestamp = str(source.get("created_at", "")).replace("+00:00", "Z")
    created = None
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            created = datetime.datetime.strptime(timestamp, pattern).replace(tzinfo=datetime.timezone.utc)
            break
        except ValueError:
            pass
    if (
        created is None
        or not -300 <= (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() <= 93600
    ):
        raise ValueError("A fresh local backup receipt is required")
    if initialize:
        invoke(
            config,
            [
                "init",
                "--from-repo",
                "/source",
                "--from-password-file",
                "/run/secrets/source-password",
                "--copy-chunker-params",
            ],
        )
    invoke(
        config,
        ["copy", "--from-repo", "/source", "--from-password-file", "/run/secrets/source-password", snapshot],
    )
    values = json.loads(invoke(config, ["snapshots", "--json"]))
    matches = [item for item in values if item.get("original") == snapshot or item.get("id") == snapshot]
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0].get("id", "")):
        raise ValueError("Exact copied snapshot receipt missing")
    invoke(config, ["check", "--read-data"])
    return {
        "schema": 1,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_snapshot_id": snapshot,
        "destination_snapshot_id": matches[0]["id"],
        "offsite_confirmed": True,
        "full_repository_check": True,
        "retention_deletes_enabled": False,
    }


def cleanup_owned_container():
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "agent-hub-purpose"}}',
            "agent-hub-offsite-copy",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip() == b"offsite-copy":
        subprocess.run(
            ["docker", "rm", "-f", "agent-hub-offsite-copy"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--init-destination", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Operator account required")
    os.umask(0o077)
    import fcntl

    if any(item.is_symlink() for item in [BASE, *BASE.parents, BASE / "backup.lock"]):
        raise ValueError("Real backup paths required")
    with (BASE / "backup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.cleanup:
                return
            raise
        if args.cleanup:
            cleanup_owned_container()
            return
        config = configuration(BASE / "offsite.json")
        result = copy_snapshot(config, args.init_destination)
        descriptor, temporary = tempfile.mkstemp(dir=str(BASE), prefix="offsite-status-")
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(result, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, str(BASE / "last-offsite.json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Encrypted offsite backup did not complete; previous confirmed receipt retained.")
        raise SystemExit(1) from None
