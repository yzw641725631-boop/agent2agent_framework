"""Portable container operations; PostgreSQL and restic are used without a Docker socket."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import UTC
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import database_checkpoint as checkpoint
import psycopg
from agent_hub import SCHEMA_REVISION, __version__
from agent_hub.archive_maintenance import audit as audit_archives
from agent_hub.config import Settings
from agent_hub.db import session_factory
from agent_hub.memory_maintenance import audit as audit_memory
from agent_hub.release_monitor import verify as verify_release
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BACKUPS = Path("/backups")
STAGING = Path("/staging")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def secret(name):
    return (Path("/run/secrets") / name).read_text().strip()


def save(path, value):
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_success(name, result):
    result.setdefault("created_at", dt.datetime.now(UTC).isoformat())
    save(BACKUPS / ("last-" + name + ".json"), result)
    (BACKUPS / ("failed-" + name + ".json")).unlink(missing_ok=True)
    return result


def command(args, *, env=None, timeout=1800, output=None):
    result = subprocess.run(args, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=timeout, check=False)
    if result.returncode:
        diagnostic = STAGING / "failed-command.log"
        diagnostic.write_bytes((result.stdout or b"") + result.stderr)
        diagnostic.chmod(0o600)
        raise RuntimeError("Operation failed: " + Path(args[0]).name)
    return result.stdout.decode() if output is None else ""


def postgres_environment():
    return {**os.environ, "PGHOST": "db", "PGPORT": "5432", "PGUSER": "postgres",
            "PGPASSWORD": secret("postgres_password"), "PGCONNECT_TIMEOUT": "10"}


def connect(database="postgres", *, autocommit=True):
    return psycopg.connect(host="db", dbname=database, user="postgres", port=5432,
                           password=secret("postgres_password"), connect_timeout=10, autocommit=autocommit)


class NativeDatabase(checkpoint.Database):
    def __init__(self, database):
        if not re.fullmatch(r"agent_hub|hub_restore_[a-f0-9]{24}", database):
            raise RuntimeError("Invalid database target")
        self.database = database
        self.command = ["psql", "-X", "-qAt", "-h", "db", "-U", "postgres", "-d", database,
                        "-v", "ON_ERROR_STOP=1"]
        os.environ.update(postgres_environment())

    def dump(self, path, snapshot):
        if not checkpoint.SNAPSHOT.fullmatch(snapshot):
            raise RuntimeError("Invalid snapshot")
        with path.open("xb") as output:
            command(["pg_dump", "-h", "db", "-U", "postgres", "-d", self.database,
                     "-Fc", "--snapshot=" + snapshot], env=postgres_environment(), output=output)
            output.flush()
            os.fsync(output.fileno())


def restic(*args, source=False):
    environment = {**os.environ, "RESTIC_REPOSITORY": "/restore-source" if source else "/backups/repository",
                   "RESTIC_PASSWORD_FILE": "/run/secrets/restore_password" if source else "/run/secrets/restic_password",
                   "GOMEMLIMIT": "192MiB"}
    # The import source is a private, quiescent copy mounted read-only.
    options = ["--no-lock"] if source else []
    return command(["restic", "--no-cache", *options, *args], env=environment, timeout=3600)


@contextlib.contextmanager
def maintenance():
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    with (BACKUPS / "maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextlib.contextmanager
def staging():
    STAGING.mkdir(mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="portable-", dir=STAGING))
    try:
        yield directory
    finally:
        if directory.parent.resolve() != STAGING.resolve() or directory.is_symlink():
            raise RuntimeError("Unexpected staging directory")
        shutil.rmtree(directory)


def download(url, target, expected, maximum):
    if target.is_file() and digest(target) == expected:
        return
    if target.exists():
        raise RuntimeError("Existing asset does not match its pinned hash: " + target.name)
    if not url.startswith("https://"):
        raise RuntimeError("Asset URL requires HTTPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = Path("/asset-cache") / ("model-fp32" if target.parent == Path("/model") else "windows-x64") / target.name
    if cache.is_file():
        if cache.stat().st_size > maximum or digest(cache) != expected:
            raise RuntimeError("Local asset cache does not match its pinned hash")
        shutil.copyfile(cache, target)
        target.chmod(0o644)
        return
    temporary = target.with_name(target.name + ".partial")
    count = 0
    with urlopen(url, timeout=30) as response, temporary.open("wb") as stream:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            count += len(block)
            if count > maximum:
                raise RuntimeError("Asset exceeds its pinned size")
            stream.write(block)
    if digest(temporary) != expected:
        temporary.unlink()
        raise RuntimeError("Asset hash mismatch")
    temporary.chmod(0o644)
    os.replace(temporary, target)


def publish_certificate(certificate, key):
    host = os.environ["HUB_PUBLIC_HOST"]
    parsed = x509.load_pem_x509_certificate(certificate)
    private = serialization.load_pem_private_key(key, password=None)
    if private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != parsed.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise RuntimeError("TLS certificate and key do not match")
    if parsed.not_valid_after_utc <= dt.datetime.now(UTC):
        raise RuntimeError("TLS certificate expired")
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    try:
        identity = ipaddress.ip_address(host)
        if identity not in names.get_values_for_type(x509.IPAddress):
            raise RuntimeError("TLS IP does not match the configured origin")
    except ValueError:
        if host not in names.get_values_for_type(x509.DNSName):
            raise RuntimeError("TLS DNS name does not match the configured origin")
    root = Path("/tls")
    generation = root / ("prepared-" + uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    for name, data in [("fullchain.pem", certificate), ("privkey.pem", key)]:
        (generation / name).write_bytes(data)
        (generation / name).chmod(0o600)
    link = root / "current.next"
    link.symlink_to(generation.name)
    os.replace(link, root / "current")


def prepare():
    root = Path("/deployment")
    assets = json.loads((root / "assets.lock.json").read_text())
    model_file = root / "model-manifest.json"
    model = json.loads(model_file.read_text())
    canonical = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != assets["model_manifest_canonical_sha256"]:
        raise RuntimeError("Model manifest changed without updating the asset lock")
    locked_model = {item["file"]: item for item in assets["model_files"]}
    if set(locked_model) != set(model["files"]):
        raise RuntimeError("Deployment model file set differs from the trusted model manifest")
    for name, expected in model["files"].items():
        if Path(name).name != name:
            raise RuntimeError("Invalid model file path")
        item = locked_model[name]
        if item["sha256"] != expected:
            raise RuntimeError("Deployment model hash differs from the trusted model manifest")
        download(item["url"], Path("/model") / name, expected, item["bytes"])
        if (Path("/model") / name).stat().st_size != item["bytes"]:
            raise RuntimeError("Model asset size mismatch")
    save(Path("/model/manifest.json"), model)
    Path("/model/manifest.json").chmod(0o644)
    stable = root / "windows-stable.json"
    if digest(stable) != assets["stable_sha256"]:
        raise RuntimeError("Desktop release envelope changed")
    # A pinned installer remains deployable after its historical update announcement expires.
    # Actual clients still verify the original expiry when using this feed for live updates.
    release = verify_release(stable.read_bytes(), time=dt.datetime.fromisoformat(assets["stable_issued_at"]))
    if release["version"] != assets["desktop_version"]:
        raise RuntimeError("Desktop version mismatch")
    directory = Path("/updates/win-x64")
    directory.mkdir(exist_ok=True)
    # These are public downloads served by Nginx's unprivileged worker.
    Path("/updates").chmod(0o755)
    directory.chmod(0o755)
    for item in assets["desktop_files"]:
        if Path(item["file"]).name != item["file"]:
            raise RuntimeError("Invalid desktop asset path")
        download(item["url"], directory / item["file"], item["sha256"], item["bytes"])
        if (directory / item["file"]).stat().st_size != item["bytes"]:
            raise RuntimeError("Desktop asset size mismatch")
    shutil.copyfile(stable, directory / "stable.json")
    (directory / "stable.json").chmod(0o644)
    os.chown("/work", 10001, 10001)
    os.chmod("/work", 0o700)
    if not Path("/tls/current").exists():
        if os.environ["HUB_TLS_MODE"] == "existing":
            publish_certificate(Path("/tls-input/fullchain.pem").read_bytes(), Path("/tls-input/privkey.pem").read_bytes())
        else:
            host = os.environ["HUB_PUBLIC_HOST"]
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            try:
                san = x509.IPAddress(ipaddress.ip_address(host))
            except ValueError:
                san = x509.DNSName(host)
            now = dt.datetime.now(UTC)
            certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                           .public_key(key.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=7))
                           .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                           .sign(key, hashes.SHA256()))
            publish_certificate(certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return {"passed": True, "model": model["model_id"], "desktop": release["version"],
            "tls_mode": os.environ["HUB_TLS_MODE"], "assets_verified": True}


def backup():
    with maintenance(), staging() as stage:
        directory = stage / "checkpoint"
        directory.mkdir(mode=0o700)
        database = NativeDatabase("agent_hub")
        with database.snapshot() as snapshot:
            database.dump(directory / "database.dump", snapshot)
            inventory = database.inventory(snapshot)
        save(directory / "inventory.json", inventory)
        save(directory / "manifest.json", {"schema": 1, "application_version": __version__,
             "database_schema": SCHEMA_REVISION, "consistent_snapshot": True,
             "database_sha256": digest(directory / "database.dump"),
             "inventory_sha256": digest(directory / "inventory.json")})
        if not (BACKUPS / "repository/config").exists():
            restic("init")
        output = restic("backup", "--json", "--tag", "agent-hub-portable", str(directory),
                        "/work", "/model", "/updates", "/config")
        rows = [json.loads(line) for line in output.splitlines()]
        summary = next(row for row in rows if row.get("message_type") == "summary")
        restic("check", "--read-data")
        result = {"passed": True, "snapshot_id": summary["snapshot_id"], "consistent_snapshot": True,
                  "full_repository_check": True, "database_schema": SCHEMA_REVISION,
                  "application_version": __version__, "created_at": dt.datetime.now(UTC).isoformat(),
                  "tables": len(inventory["tables"]), "rows": sum(item["rows"] for item in inventory["tables"]),
                  "offsite_confirmed": False}
        return record_success("backup", result)


def settings(database="agent_hub", *, key=None, work="/work"):
    url = "postgresql+psycopg://hub_owner:" + quote(secret("owner_password"), safe="") + "@db:5432/" + database
    return Settings(database_url=url, secret=key or secret("application_key"), artifact_directory=work,
                    environment="test", mail_mode="memory", public_url="http://127.0.0.1:8765")


def archive_audit(database="agent_hub", *, key=None, work="/work"):
    cfg = settings(database, key=key, work=work)
    factory = session_factory(cfg)
    try:
        archives = audit_archives(cfg, factory)
        memory = audit_memory(factory)
        if not archives["healthy"] or not memory["healthy"]:
            raise RuntimeError("Archive or memory audit failed")
        return {"passed": True, "recorded_archives": archives["recorded"],
                "verified_archives": archives["verified"], "memory_healthy": memory["healthy"]}
    finally:
        factory.kw["bind"].dispose()


def audit():
    with maintenance():
        return record_success("audit", archive_audit())


def load_backup(stage, snapshot, *, source=False):
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot):
        raise RuntimeError("Use the exact confirmed snapshot ID")
    restic("restore", snapshot, "--target", str(stage), source=source)
    candidates = list(stage.glob("staging/*/checkpoint/manifest.json"))
    if len(candidates) != 1:
        raise RuntimeError("Backup checkpoint manifest is missing or ambiguous")
    directory = candidates[0].parent
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Links are not accepted in a deployment backup")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["application_version"] != __version__ or manifest["database_schema"] != SCHEMA_REVISION:
        raise RuntimeError("Restore requires the matching application and database version")
    if not manifest["consistent_snapshot"] or digest(directory / "database.dump") != manifest["database_sha256"]:
        raise RuntimeError("Backup dump verification failed")
    if digest(directory / "inventory.json") != manifest["inventory_sha256"]:
        raise RuntimeError("Backup inventory verification failed")
    expected = checkpoint.baseline(json.loads((directory / "inventory.json").read_text()))
    return directory, expected


def restore_database(name, dump):
    if not re.fullmatch(r"agent_hub|hub_restore_[a-f0-9]{24}", name):
        raise RuntimeError("Invalid restore database")
    with connect() as db:
        db.execute(psycopg.sql.SQL("CREATE DATABASE {} OWNER hub_owner TEMPLATE template0").format(psycopg.sql.Identifier(name)))
    command(["pg_restore", "-h", "db", "-U", "postgres", "-d", name, "--exit-on-error", str(dump)],
            env=postgres_environment())


def verify_database(name, expected):
    database = NativeDatabase(name)
    with database.snapshot() as snapshot:
        actual = database.inventory(snapshot)
    return checkpoint.compare(expected, actual)


def drill(snapshot=None):
    with maintenance(), staging() as stage:
        snapshot = snapshot or json.loads((BACKUPS / "last-backup.json").read_text())["snapshot_id"]
        directory, expected = load_backup(stage, snapshot)
        name = "hub_restore_" + uuid.uuid4().hex[:24]
        try:
            restore_database(name, directory / "database.dump")
            verification = verify_database(name, expected)
            key = (stage / "config/secrets/application_key").read_text().strip()
            archives = archive_audit(name, key=key, work=str(stage / "work"))
            result = {"passed": True, "snapshot_id": snapshot, "database": verification,
                      "archives": archives, "created_at": dt.datetime.now(UTC).isoformat(),
                      "offsite_confirmed": False}
            return record_success("restore", result)
        finally:
            # Also clean up a database left by an interrupted pg_restore.
            with connect() as db:
                db.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(psycopg.sql.Identifier(name)))


def fresh_target():
    with connect("agent_hub") as db:
        counts = db.execute("SELECT (SELECT count(*) FROM users)+(SELECT count(*) FROM projects)+"
                            "(SELECT count(*) FROM worklogs)+(SELECT count(*) FROM work_snapshots)").fetchone()[0]
        if counts:
            raise RuntimeError("Restore only accepts a fresh target with no users, projects, logs or snapshots")
    if any(Path("/work").iterdir()):
        raise RuntimeError("Restore requires an empty target archive volume")
    return {"passed": True, "fresh_target_confirmed": True}


def restore_live(snapshot):
    with maintenance(), staging() as stage:
        directory, expected = load_backup(stage, snapshot, source=True)
        fresh_target()
        with connect() as db:
            db.execute("DROP DATABASE agent_hub WITH (FORCE)")
        restore_database("agent_hub", directory / "database.dump")
        verification = verify_database("agent_hub", expected)
        # Only committed snapshots are needed; temporary uploads and quarantined orphans stay in the backup.
        with connect("agent_hub") as db:
            snapshot_ids = [str(row[0]) for row in db.execute("SELECT id FROM work_snapshots")]
        for snapshot_id in snapshot_ids:
            source = stage / "work" / (str(uuid.UUID(snapshot_id)) + ".bin")
            if not source.is_file():
                raise RuntimeError("A recorded archive is missing from the backup")
            destination = Path("/work") / source.name
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            os.chown(destination, 10001, 10001)
        key = (stage / "config/secrets/application_key").read_text().strip()
        archives = archive_audit(key=key)
        # The archive audit above uses the source key; retain new target database passwords.
        target = Path("/config/secrets/application_key")
        temporary = target.with_name("application_key.next")
        temporary.write_text(key + "\n")
        temporary.chmod(0o444)
        os.replace(temporary, target)
        result = {"passed": True, "snapshot_id": snapshot, "database": verification, "archives": archives,
                  "fresh_target_confirmed": True, "source_application_key_restored": True,
                  "target_database_passwords_retained": True, "created_at": dt.datetime.now(UTC).isoformat()}
        save(BACKUPS / "last-import.json", result)
        return result


def check():
    with connect("agent_hub") as db:
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if revision != SCHEMA_REVISION:
            raise RuntimeError("Database revision does not match the application")
    for origin in ("http://api:8765", "http://embedding:8766"):
        with urlopen(origin + "/healthz", timeout=10) as response:
            value = json.loads(response.read(8192))
        healthy = value.get("status") == "ok" if origin.endswith("8765") else (
            value.get("dimensions") == 512 and value.get("model") == "bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1")
        if not healthy:
            raise RuntimeError("Service health check failed")
    return {"passed": True, "database_schema": revision, "version": __version__,
            "scope": "database-and-internal-services", "created_at": dt.datetime.now(UTC).isoformat(),
            "offsite_confirmed": False, "external_alert_delivery_configured": False}


def schedule():
    jobs = [("check", 300, "last-check.json", check),
            ("backup", int(os.environ["HUB_BACKUP_INTERVAL_SECONDS"]), "last-backup.json", backup),
            ("audit", int(os.environ["HUB_AUDIT_INTERVAL_SECONDS"]), "last-audit.json", audit),
            ("restore", int(os.environ["HUB_RESTORE_INTERVAL_SECONDS"]), "last-restore.json", drill)]
    while True:
        for name, interval, filename, action in jobs:
            target = BACKUPS / filename
            if target.exists() and time.time() - target.stat().st_mtime < interval:
                continue
            try:
                result = action()
                result.setdefault("created_at", dt.datetime.now(UTC).isoformat())
                save(target, result)
                (BACKUPS / ("failed-" + name + ".json")).unlink(missing_ok=True)
                print(json.dumps({"job": name, "passed": True}), flush=True)
            except Exception as error:  # noqa: BLE001 - persist a redacted failure and keep later jobs running
                save(BACKUPS / ("failed-" + name + ".json"),
                     {"passed": False, "error_type": type(error).__name__, "created_at": dt.datetime.now(UTC).isoformat()})
                print(json.dumps({"job": name, "passed": False, "error_type": type(error).__name__}), flush=True)
        time.sleep(60)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "backup", "drill", "audit", "check", "schedule", "fresh-target", "restore-live"))
    parser.add_argument("snapshot", nargs="?")
    args = parser.parse_args()
    if args.operation == "schedule":
        schedule()
        return
    actions = {"prepare": prepare, "backup": backup, "drill": lambda: drill(args.snapshot),
               "audit": audit, "check": check, "fresh-target": fresh_target,
               "restore-live": lambda: restore_live(args.snapshot or "")}
    print(json.dumps(actions[args.operation](), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - do not expose subprocess diagnostics or business data
        diagnostic = STAGING / "failed-operation.log"
        if STAGING.is_dir():
            diagnostic.write_text(traceback.format_exc())
            diagnostic.chmod(0o600)
        # Commands and database diagnostics may contain credentials or business data.
        print(json.dumps({"passed": False, "error_type": type(error).__name__, "operation_failed": True}),
              file=sys.stderr, flush=True)
        raise SystemExit(1) from None
