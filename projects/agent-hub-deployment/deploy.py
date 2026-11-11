"""Deploy the complete Agent Hub stack on a Linux Docker host using this checkout."""

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
SECRET_NAMES = ("postgres_password", "owner_password", "runtime_password", "document_password",
                "memory_password", "embedding_password", "generation_password", "application_key",
                "bootstrap_token", "restic_password")
APPLICATIONS = ["api", "document-worker", "memory-worker", "embedding-worker", "generation-worker",
                "embedding", "gateway", "scheduler"]
PROTECTED_PROJECTS = {"agent-hub-app", "agent-hub-data", "agent-hub-edge"}


class AssetHTTPSConnection(http.client.HTTPSConnection):
    """Try each DNS address through TLS; a reachable TCP socket may still stall at TLS."""

    def connect(self):
        if self._tunnel_host:
            return super().connect()
        last_error = None
        for family, kind, protocol, _, address in socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM):
            raw = socket.socket(family, kind, protocol)
            raw.settimeout(min(self.timeout, 8))
            try:
                raw.connect(address)
                # Keep the original hostname for SNI and normal certificate/hostname verification.
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                self.sock.settimeout(self.timeout)
                return None
            except OSError as error:
                raw.close()
                last_error = error
        if last_error:
            raise last_error
        raise OSError("No address is available for the HTTPS asset host")


class AssetHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(AssetHTTPSConnection, request, context=self._context)


def settings():
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Run init first")
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def run(args, *, timeout=3600, capture=True):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        log = RUNTIME / "failed-command.log"
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        raise RuntimeError("Command failed; private diagnostics: " + str(log))
    if not capture and result.stdout:
        print(result.stdout, end="", flush=True)
    return result.stdout.strip()


def compose(*args, extra=None, capture=True, timeout=3600):
    values = settings()
    command = ["docker", "compose", "--project-name", values["HUB_STACK_NAME"],
               "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "compose.yaml")]
    if (RUNTIME / "asset-cache.compose.json").exists():
        command.extend(["-f", str(RUNTIME / "asset-cache.compose.json")])
    if extra:
        command.extend(["-f", str(extra)])
    return run([*command, *args], timeout=timeout, capture=capture)


def require_host():
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Run on the target Linux x86_64 server, not the developer workstation")
    if tuple(sys.version_info[:2]) < (3, 9):
        raise RuntimeError("Python 3.9 or newer is required")
    if not shutil.which("docker"):
        raise RuntimeError("Install Docker Engine and the Compose v2 plugin first")
    version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, text=True, check=False)
    match = re.match(r"v?(\d+)\.(\d+)\.", version.stdout.strip())
    if version.returncode or not match or tuple(map(int, match.groups())) < (2, 27):
        raise RuntimeError("Docker Compose 2.27 or newer is required")


def initialize(args):
    if (ROOT / ".env").exists() or RUNTIME.exists():
        raise RuntimeError("Initialization never overwrites existing configuration or secrets")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,48}", args.name) or args.name in PROTECTED_PROJECTS:
        raise RuntimeError("Use a new stack name, separate from the existing server installation")
    for resource in ("ps", "volume", "network"):
        command = ["docker", "ps", "-aq"] if resource == "ps" else ["docker", resource, "ls", "-q"]
        result = subprocess.run([*command, "--filter", "label=com.docker.compose.project=" + args.name],
                                capture_output=True, text=True, check=True)
        if result.stdout.strip():
            raise RuntimeError("The stack name already owns Docker resources; use a new independent name")
    origin = urlsplit(args.origin)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment:
        raise RuntimeError("Origin must be a complete HTTPS origin without credentials, path or query")
    if origin.path not in {"", "/"} or any(char.isspace() for char in args.origin):
        raise RuntimeError("Origin must not contain a path or whitespace")
    host = origin.hostname.encode("idna").decode()
    if not re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
        raise RuntimeError("Invalid origin hostname")
    port = origin.port or 443
    if not 1 <= port <= 65535 or not 1 <= args.http_port <= 65535 or port == args.http_port:
        raise RuntimeError("HTTP and HTTPS ports must be different valid ports")
    try:
        ipaddress.ip_address(args.bind)
    except ValueError as error:
        raise RuntimeError("Bind address must be an IP address") from error
    try:
        ipaddress.ip_address(host)
        identifier, profile = "--ip-address", "shortlived"
    except ValueError:
        identifier, profile = "-d", "classic"
    if args.tls == "acme" and (not args.email or args.http_port != 80):
        raise RuntimeError("ACME mode requires an email address and public HTTP port 80")
    if args.email and not re.fullmatch(r"[A-Za-z0-9.!%+_-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", args.email):
        raise RuntimeError("Use a valid email address without whitespace or Compose variable syntax")
    if args.tls == "existing" and (not args.cert or not args.key):
        raise RuntimeError("Existing TLS mode requires --cert and --key")
    if args.tls == "local-test" and (not args.test_mode or args.bind not in {"127.0.0.1", "::1"}):
        raise RuntimeError("Self-signed mode requires explicit --test-mode and a loopback bind address")
    RUNTIME.mkdir(mode=0o700)
    directory = RUNTIME / "secrets"
    directory.mkdir(mode=0o700)
    for name in SECRET_NAMES:
        path = directory / name
        path.write_text(secrets.token_urlsafe(48) + "\n")
        # Compose mounts these files individually. The parent directory stays private on the host.
        path.chmod(0o444)
    tls = RUNTIME / "tls-input"
    tls.mkdir(mode=0o700)
    if args.tls == "existing":
        for source, name in [(args.cert, "fullchain.pem"), (args.key, "privkey.pem")]:
            if not source.is_file():
                raise RuntimeError("TLS input file does not exist")
            shutil.copyfile(source, tls / name)
            (tls / name).chmod(0o444)
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    values.update(HUB_STACK_NAME=args.name, HUB_PUBLIC_URL=args.origin.rstrip("/"), HUB_PUBLIC_HOST=host,
                  HUB_HTTP_BIND="[" + args.bind + "]" if ":" in args.bind else args.bind,
                  HUB_HTTP_PORT=str(args.http_port), HUB_HTTPS_PORT=str(port),
                  HUB_TLS_MODE=args.tls, HUB_ACME_EMAIL=args.email or "", HUB_ACME_IDENTIFIER_FLAG=identifier,
                  HUB_ACME_PROFILE=profile, HUB_ENVIRONMENT="test" if args.test_mode else "production",
                  HUB_MAIL_MODE="memory" if args.test_mode else "smtp",
                  HUB_BACKEND_IMAGE=args.name + "-api:0.22.0",
                  HUB_DATABASE_IMAGE=args.name + "-postgres:18.6-vector0.8.7",
                  HUB_EMBEDDING_IMAGE=args.name + "-embedding:0.1.0", HUB_OPERATIONS_IMAGE=args.name + "-ops:0.22.0")
    (ROOT / ".env").write_text("# Generated settings; secrets are separate Compose mounts.\n" +
                               "".join(key + "=" + value + "\n" for key, value in values.items()))
    (ROOT / ".env").chmod(0o600)
    template = (ROOT / "templates/nginx.conf").read_text().replace("@@PUBLIC_URL@@", values["HUB_PUBLIC_URL"])
    (RUNTIME / "nginx.conf").write_text(template)
    (RUNTIME / "nginx.conf").chmod(0o644)
    mounts = []
    for source, target in [(args.model_cache, "/asset-cache/model-fp32"),
                           (args.desktop_cache, "/asset-cache/windows-x64")]:
        if source:
            source = source.resolve(strict=True)
            if not source.is_dir():
                raise RuntimeError("Asset cache must be a directory")
            mounts.append({"type": "bind", "source": str(source), "target": target, "read_only": True})
    if mounts:
        (RUNTIME / "asset-cache.compose.json").write_text(json.dumps({"services": {"setup": {"volumes": mounts}}}))
    compose("config", "--quiet")
    print("Initialized an independent stack. Secrets remain under .runtime/secrets/; existing configuration was not read.")


def build():
    for services in [("db", "api", "embedding"), ("operations",)]:
        print("Building Docker images: " + ", ".join(services), flush=True)
        compose("--profile", "tools", "build", *services)
    print("Docker images built from the checked-out source.", flush=True)


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fetch_assets():
    """Use the host download path, then let the setup container recheck every byte."""
    if (RUNTIME / "asset-cache.compose.json").exists():
        return
    lock = json.loads((ROOT / "assets.lock.json").read_text())
    model = json.loads((ROOT.parent / "agent-hub-embedding/model-manifest.json").read_text())
    canonical = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != lock["model_manifest_canonical_sha256"]:
        raise RuntimeError("Model manifest does not match the pinned deployment")
    groups = [("model-fp32", lock["model_files"]), ("windows-x64", lock["desktop_files"])]
    opener = build_opener(AssetHTTPSHandler())
    mounts = []
    for name, items in groups:
        directory = RUNTIME / "assets" / name
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for item in items:
            if Path(item["file"]).name != item["file"] or not item["url"].startswith("https://"):
                raise RuntimeError("Invalid pinned asset")
            if name == "model-fp32" and model["files"].get(item["file"]) != item["sha256"]:
                raise RuntimeError("Model asset does not match the trusted model manifest")
            target = directory / item["file"]
            if target.is_file() and target.stat().st_size == item["bytes"] and file_digest(target) == item["sha256"]:
                continue
            print("Fetching pinned asset: " + item["file"], flush=True)
            temporary = target.with_name(target.name + ".partial")
            endpoints = list(dict.fromkeys(filter(None, [item.get("api_url"), item["url"]])))
            for index, endpoint in enumerate(endpoints):
                if not endpoint.startswith("https://"):
                    raise RuntimeError("Asset download requires HTTPS")
                count = 0
                value = hashlib.sha256()
                request = Request(endpoint, headers={"Accept": "application/octet-stream",
                                  "X-GitHub-Api-Version": "2022-11-28"})
                try:
                    with opener.open(request, timeout=30) as response, temporary.open("wb") as stream:
                        for block in iter(lambda: response.read(1024 * 1024), b""):
                            count += len(block)
                            if count > item["bytes"]:
                                raise RuntimeError("Downloaded asset exceeds its pinned length")
                            stream.write(block)
                            value.update(block)
                        stream.flush()
                        os.fsync(stream.fileno())
                    break
                except OSError:
                    if index == len(endpoints) - 1:
                        raise RuntimeError("Cannot download " + item["file"] + "; verify server HTTPS access to GitHub Release assets") from None
            if count != item["bytes"] or value.hexdigest() != item["sha256"]:
                temporary.unlink()
                raise RuntimeError("Downloaded asset failed SHA-256/length verification")
            temporary.chmod(0o600)
            os.replace(temporary, target)
        mounts.append({"type": "bind", "source": str(directory),
                       "target": "/asset-cache/" + name, "read_only": True})
    (RUNTIME / "asset-cache.compose.json").write_text(json.dumps({"services": {"setup": {"volumes": mounts}}}))


def verify_https(*, seconds=90):
    values = settings()
    context = ssl.create_default_context()
    if values["HUB_TLS_MODE"] == "local-test":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urlopen(values["HUB_PUBLIC_URL"] + "/healthz", context=context, timeout=8) as response:
                result = json.loads(response.read(8192))
            if result.get("version") != "0.22.0" or result.get("database_schema") != "0032":
                raise RuntimeError("Unexpected deployed application version")
            lock = json.loads((ROOT / "assets.lock.json").read_text())
            download_root = values["HUB_PUBLIC_URL"] + "/desktop-updates/win-x64/"
            with urlopen(download_root + "stable.json", context=context, timeout=8) as response:
                signed_feed = response.read(65537)
            if hashlib.sha256(signed_feed).hexdigest() != lock["stable_sha256"]:
                raise RuntimeError("Published desktop release feed differs from the pinned bytes")
            for item in lock["desktop_files"]:
                with urlopen(Request(download_root + item["file"], method="HEAD"), context=context, timeout=8) as response:
                    if int(response.headers.get("Content-Length", "0")) != item["bytes"]:
                        raise RuntimeError("Published desktop download length differs from the pinned file")
            return {"passed": True, "origin": values["HUB_PUBLIC_URL"], "tls_mode": values["HUB_TLS_MODE"],
                    "publicly_trusted_tls": values["HUB_TLS_MODE"] != "local-test", "health": result,
                    "desktop_download_metadata_verified": True, "signed_feed_bytes_verified": True}
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("HTTPS/API/download verification failed; check gateway, assets, DNS and TLS") from None
            time.sleep(2)


def up(args):
    if not args.no_build:
        build()
    fetch_assets()
    print("Preparing hash-verified model, desktop downloads and TLS files.", flush=True)
    compose("--profile", "tools", "run", "--rm", "--no-deps", "setup", "prepare", capture=False)
    compose("up", "-d", "--wait", "--wait-timeout", "180", "db")
    compose("--profile", "tools", "run", "--rm", "--no-deps", "migrate", capture=False)
    print("Starting API, workers, private embeddings, gateway and container operations.", flush=True)
    compose("up", "-d", "--wait", "--wait-timeout", "240", *APPLICATIONS)
    if settings()["HUB_TLS_MODE"] == "acme":
        compose("--profile", "acme", "run", "--rm", "--no-deps", "certificates", "issue", capture=False)
        compose("--profile", "acme", "up", "-d", "certificates")
    result = verify_https()
    (RUNTIME / "deployment-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    print("First installation: open the configured origin and complete SMTP/email setup yourself. "
          "The one-time initialization token is in .runtime/secrets/bootstrap_token; do not paste it into chat.")


def restore(args):
    repository = args.repository.resolve(strict=True)
    password = args.password_file.resolve(strict=True)
    if not repository.is_dir() or not password.is_file() or not re.fullmatch(r"[a-f0-9]{64}", args.snapshot):
        raise RuntimeError("Restore requires a repository directory, password file and exact snapshot ID")
    override = RUNTIME / "restore.compose.json"
    override.write_text(json.dumps({"services": {"operations": {
        "volumes": [{"type": "bind", "source": str(repository), "target": "/restore-source", "read_only": True},
                    {"type": "volume", "source": "work", "target": "/work"},
                    {"type": "bind", "source": str(RUNTIME / "secrets"), "target": "/config/secrets"}],
        "cap_add": ["DAC_READ_SEARCH", "DAC_OVERRIDE", "CHOWN"],
        "secrets": ["restore_password"]}}, "secrets": {"restore_password": {"file": str(password)}}}))
    override.chmod(0o600)
    try:
        # Reject populated targets before stopping any service, then recheck after quiescing writes.
        compose("--profile", "tools", "run", "--rm", "--no-deps", "operations", "fresh-target", capture=False)
        compose("stop", *APPLICATIONS)
        print("Restoring the confirmed backup into a fresh target.", flush=True)
        compose("--profile", "tools", "run", "--rm", "--no-deps", "operations", "restore-live", args.snapshot,
                extra=override, capture=False)
    finally:
        override.unlink(missing_ok=True)
    # Individual secret bind mounts must be recreated after importing the source encryption key.
    compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "240", *APPLICATIONS)
    print(json.dumps(verify_https(), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    init = sub.add_parser("init", help="Generate settings and fresh secrets without overwriting existing files")
    init.add_argument("--origin", required=True)
    init.add_argument("--name", default="agent-hub-portable")
    init.add_argument("--tls", choices=("acme", "existing", "local-test"), default="acme")
    init.add_argument("--email")
    init.add_argument("--cert", type=Path)
    init.add_argument("--key", type=Path)
    init.add_argument("--bind", default="0.0.0.0")
    init.add_argument("--http-port", type=int, default=80)
    init.add_argument("--test-mode", action="store_true")
    init.add_argument("--model-cache", type=Path)
    init.add_argument("--desktop-cache", type=Path)
    sub.add_parser("build")
    start = sub.add_parser("up")
    start.add_argument("--no-build", action="store_true")
    for operation in ("check", "backup", "drill", "audit", "status", "down"):
        sub.add_parser(operation)
    recover = sub.add_parser("restore")
    recover.add_argument("--repository", required=True, type=Path)
    recover.add_argument("--password-file", required=True, type=Path)
    recover.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    require_host()
    os.umask(0o077)
    if args.operation == "init":
        initialize(args)
    elif args.operation == "build":
        build()
    elif args.operation == "up":
        up(args)
    elif args.operation == "restore":
        restore(args)
    elif args.operation in {"check", "backup", "drill", "audit"}:
        compose("--profile", "tools", "run", "--rm", "--no-deps", "operations", args.operation, capture=False)
        if args.operation == "check":
            print(json.dumps(verify_https(), ensure_ascii=False))
    elif args.operation == "status":
        compose("ps", "--all", capture=False)
    elif args.operation == "down":
        compose("--profile", "acme", "down", "--remove-orphans", capture=False)
        print("Stopped the stack; all persistent volumes and secrets were retained.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - provide a concise CLI error; private diagnostics stay on the host
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
