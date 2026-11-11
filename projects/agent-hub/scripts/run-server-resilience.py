"""Run a bounded, disposable Docker drill on the authorized Linux test server.

Never run on the developer workstation. No production volumes or networks are used.
The caller supplies existing immutable test images; this script does not deploy code.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PURPOSE = "server-resilience-test"


def run(*args, timeout=30, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Container output can include private state; retain it only in the private run directory.
        raise RuntimeError("Command failed: " + " ".join(args[:3]))
    return result.stdout.strip()


def docker(*args, **kwargs):
    return run("docker", *args, **kwargs)


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def container_logs(name):
    result = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=20)
    return result.stdout + result.stderr


def health():
    started = time.monotonic()
    value = json.loads(run("curl", "-fsS", "--max-time", "8", "https://47.109.23.86:18180/healthz"))
    if value.get("status") != "ok":
        raise RuntimeError("Production health probe failed")
    return {"healthy": True, "seconds": round(time.monotonic() - started, 3), "response": value}


def production():
    value = inspect("agent-hub-postgres")
    api = inspect("agent-hub-app-api-1")
    volume = next(item["Name"] for item in value["Mounts"] if item["Destination"] == "/var/lib/postgresql")
    if volume != "agent-hub-postgres-data" or not value["State"]["Running"]:
        raise RuntimeError("Production database identity does not match")
    return {
        "release": os.path.realpath("/opt/agent-hub/application/current"),
        "database_container_id": value["Id"],
        "database_started_at": value["State"]["StartedAt"],
        "production_data_volume": volume,
        "api_image": api["Image"],
        "api_container_id": api["Id"],
        "api_started_at": api["State"]["StartedAt"],
        "schema": docker(
            "exec",
            "agent-hub-postgres",
            "psql",
            "-X",
            "-qAt",
            "-U",
            "postgres",
            "-d",
            "agent_hub",
            "-c",
            "SELECT version_num FROM alembic_version",
        ),
        "gateway": health(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable-server-test", action="store_true", required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Run only on the authorized Linux test server")
    if args.report.exists():
        raise SystemExit("Report must not overwrite an existing file")
    source = Path(__file__).resolve().parents[1]
    if source.is_relative_to(Path("/opt/agent-hub/application/releases")):
        raise SystemExit("Use an isolated source checkout, not a production release directory")
    for image in (args.postgres_image, args.runtime_image):
        if not image.startswith("sha256:") or len(image) != 71:
            raise SystemExit("Supply an existing immutable sha256 image ID")
        docker("image", "inspect", image)
    memory = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    if memory < 1400 * 1024 * 1024 or shutil.disk_usage(source).free < 2 * 1024**3:
        raise SystemExit("At least 1400 MiB available memory and 2 GiB free disk are required")
    before = production()
    previous_test_running = inspect("agent-hub-complete-test-db")["State"]["Running"]
    prefix = "agent-hub-resilience-" + uuid.uuid4().hex[:12]
    workspace = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=source.parent))
    workspace.chmod(0o700)
    state = workspace / "state"
    state.mkdir(mode=0o700)
    os.chown(state, 10001, 10001)
    environment = workspace / "test.env"
    values = {
        "POSTGRES_DB": "hub_test",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "HUB_SECRET": secrets.token_urlsafe(48),
        "HUB_SERVER_RESILIENCE": "disposable-only",
        "HUB_ENVIRONMENT": "test",
        "HUB_MAIL_MODE": "memory",
        "PYTHONPATH": "/app/src:/app/tests",
        "HUB_MEMORY_TOKENIZER_FILE": "/app/cl100k_base.tiktoken",
    }
    for role in ("OWNER", "RUNTIME", "INDEXER", "MEMORY", "EMBEDDING", "GENERATION"):
        values["HUB_" + role + "_PASSWORD"] = secrets.token_urlsafe(40)
    with environment.open("x") as stream:
        environment.chmod(0o600)
        stream.write("".join(key + "=" + value + "\n" for key, value in values.items()))
    names = {key: prefix + "-" + key for key in ("db", "api", "seed", "driver")}
    network, volume = prefix + "-net", prefix + "-data"
    created = []
    report = {
        "passed": False,
        "started_unix": time.time(),
        "host": "server47",
        "baseline_production": before,
        "memory_available_before_bytes": memory,
        "resource_limits": {
            "api": {"memory_mib": 512, "cpus": 0.75},
            "database": {"memory_mib": 384, "cpus": 0.5},
            "driver": {"memory_mib": 192, "cpus": 0.25},
        },
        "swap_disabled": True,
        "network_internal": True,
        "published_ports": [],
        "images": {"postgres": args.postgres_image, "runtime": args.runtime_image},
        "source_sha256": {
            path: hashlib.sha256((source / path).read_bytes()).hexdigest()
            for path in (
                "scripts/run-server-resilience.py",
                "tests/server_resilience_fixture.py",
                "src/agent_hub/api.py",
                "src/agent_hub/db.py",
                "src/agent_hub/tasks.py",
                "src/agent_hub/work_archives.py",
            )
        },
        "production_probes": [],
        "resource_samples": [],
    }
    label = ["--label", "agent-hub-purpose=" + PURPOSE, "--label", "agent-hub-test-run=" + prefix]

    def owned(name):
        obj = inspect(name)
        if obj["Config"].get("Labels", {}).get("agent-hub-test-run") != prefix:
            raise RuntimeError("Refusing an operation on an unowned container")
        if "agent-hub-database" in obj["NetworkSettings"]["Networks"]:
            raise RuntimeError("Disposable container attached to production network")
        if any(mount.get("Name") == "agent-hub-postgres-data" for mount in obj["Mounts"]):
            raise RuntimeError("Disposable container attached to production data")
        return obj

    def create_python(kind, memory_mib, cpus, mode):
        docker(
            "create",
            "--name",
            names[kind],
            *label,
            "--network",
            network,
            *(["--network-alias", "api"] if kind == "api" else []),
            "--user",
            "10001:10001",
            "--memory",
            str(memory_mib) + "m",
            "--memory-swap",
            str(memory_mib) + "m",
            "--cpus",
            str(cpus),
            "--env-file",
            str(environment),
            "--workdir",
            "/app",
            "--mount",
            "type=bind,source=" + str(source) + ",target=/app,readonly",
            "--mount",
            "type=bind,source=" + str(state) + ",target=/state",
            "--entrypoint",
            "python",
            args.runtime_image,
            "-B",
            "tests/server_resilience_fixture.py",
            mode,
        )
        created.append(names[kind])
        owned(names[kind])
        docker("start", names[kind])

    def signal(name, value):
        target = state / name
        target.write_text(json.dumps(value) + "\n")
        target.chmod(0o600)
        os.chown(target, 10001, 10001)

    last_probe = 0

    def monitor():
        nonlocal last_probe
        if time.monotonic() - last_probe < 10:
            return
        last_probe = time.monotonic()
        report["production_probes"].append(health())
        active = [name for name in created if inspect(name)["State"]["Running"]]
        if active:
            rows = docker("stats", "--no-stream", "--format", "{{json .}}", *active).splitlines()
            report["resource_samples"].append(
                {"unix": time.time(), "containers": [json.loads(row) for row in rows]}
            )

    def wait_for(predicate, seconds, message):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            monitor()
            if names["driver"] in created and not inspect(names["driver"])["State"]["Running"]:
                if not (state / "result.json").exists():
                    raise RuntimeError("Driver stopped without a result")
                if not json.loads((state / "result.json").read_text()).get("passed"):
                    raise RuntimeError("Drill failed; private diagnostics retained")
            time.sleep(0.25)
        raise RuntimeError(message)

    try:
        docker("network", "create", "--internal", *label, network)
        docker("volume", "create", *label, volume)
        docker(
            "create",
            "--name",
            names["db"],
            *label,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--memory",
            "384m",
            "--memory-swap",
            "384m",
            "--cpus",
            "0.5",
            "--env-file",
            str(environment),
            "--mount",
            "type=volume,source=" + volume + ",target=/var/lib/postgresql",
            "--mount",
            "type=bind,source="
            + str(source / "deploy/01_roles.sql")
            + ",target=/docker-entrypoint-initdb.d/01_roles.sql,readonly",
            args.postgres_image,
            "postgres",
            "-c",
            "shared_buffers=32MB",
            "-c",
            "max_connections=40",
        )
        created.append(names["db"])
        owned(names["db"])
        docker("start", names["db"])

        def database_ready():
            return (
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        names["db"],
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "postgres",
                        "-d",
                        "hub_test",
                    ],
                    capture_output=True,
                    timeout=5,
                ).returncode
                == 0
            )

        wait_for(database_ready, 60, "Disposable database did not start")
        docker(
            "exec",
            names["db"],
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "hub_test",
            "-c",
            "CREATE EXTENSION vector",
        )
        create_python("seed", 512, 0.75, "seed")
        wait_for(lambda: not inspect(names["seed"])["State"]["Running"], 180, "Fixture seeding timed out")
        (workspace / "seed.log").write_text(container_logs(names["seed"]))
        if inspect(names["seed"])["State"]["ExitCode"] != 0:
            raise RuntimeError("Fixture seeding failed; private diagnostics retained")
        create_python("api", 512, 0.75, "serve")

        def api_ready():
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    names["api"],
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18768/healthz',timeout=2).read()",
                ],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0

        wait_for(api_ready, 40, "Disposable API did not start")
        create_python("driver", 192, 0.25, "drive")
        for kind, resource in (("api", "api"), ("database", "db")):
            wait_for(
                lambda kind=kind: (state / ("await-" + kind + ".json")).exists(),
                480,
                "Driver did not reach the fault checkpoint",
            )
            name = names[resource]
            old = owned(name)
            surviving_api_pid = owned(names["api"])["State"]["Pid"]
            docker("kill", "--signal", "KILL", name)
            assert not owned(name)["State"]["Running"]
            signal(kind + "-down.json", {"stopped": True})
            wait_for(
                lambda kind=kind: (state / (kind + "-down-observed.json")).exists(),
                60,
                "Driver did not observe the outage",
            )
            docker("start", name)
            if resource == "db":
                wait_for(database_ready, 60, "Database did not recover")
            restarted = owned(name)
            assert restarted["State"]["Pid"] != old["State"]["Pid"]
            value = {
                "signal": "SIGKILL",
                "new_main_pid": restarted["State"]["Pid"],
                "old_main_pid": old["State"]["Pid"],
                "same_container": restarted["Id"] == old["Id"],
                "distinct_main_process": restarted["State"]["Pid"] != old["State"]["Pid"],
            }
            if resource == "db":
                assert owned(names["api"])["State"]["Pid"] == surviving_api_pid
                log = container_logs(name)
                value.update(
                    api_process_preserved=True,
                    wal_crash_recovery_observed="database system was interrupted" in log
                    and "redo starts at" in log,
                )
                assert value["wal_crash_recovery_observed"]
            signal(kind + "-restarted.json", value)
        wait_for(lambda: (state / "result.json").exists(), 90, "Final result not produced")
        wait_for(lambda: not owned(names["driver"])["State"]["Running"], 20, "Driver did not exit")
        report["drill"] = json.loads((state / "result.json").read_text())
        assert report["drill"]["passed"] and owned(names["driver"])["State"]["ExitCode"] == 0
        assert all(not owned(name)["State"]["OOMKilled"] for name in created)
        report["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        print("Drill incomplete: " + str(error), flush=True)
    finally:
        if "drill" not in report:
            for partial in ("result.json", "progress.json"):
                if (state / partial).is_file():
                    report["drill"] = json.loads((state / partial).read_text())
                    break
        for name in created:
            obj = owned(name)
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=15)
            (workspace / (name + ".log")).write_text(logs.stdout + logs.stderr)
            report.setdefault("container_final_states", []).append(
                {
                    "kind": next(key for key, item in names.items() if item == name),
                    "exit_code": obj["State"]["ExitCode"],
                    "oom_killed": obj["State"]["OOMKilled"],
                }
            )
            if obj["State"]["Running"]:
                docker("stop", "--time", "10", name)
            docker("rm", name)
        volume_info = json.loads(docker("volume", "inspect", volume))[0]
        if (
            volume_info.get("Labels", {}).get("agent-hub-test-run") != prefix
            or volume == "agent-hub-postgres-data"
        ):
            raise RuntimeError("Refusing to remove an unowned volume")
        docker("volume", "rm", volume)
        network_info = json.loads(docker("network", "inspect", network))[0]
        if network_info.get("Labels", {}).get("agent-hub-test-run") != prefix:
            raise RuntimeError("Refusing to remove an unowned network")
        docker("network", "rm", network)
        environment.unlink()
        if workspace.resolve().parent != source.parent.resolve() or not workspace.name.startswith(
            prefix + "-"
        ):
            raise RuntimeError("Private workspace identity changed")
        # State contains only this run's synthetic device keys and archives.
        if state.resolve() != workspace.resolve() / "state":
            raise RuntimeError("Refusing cleanup of an unexpected private state path")
        shutil.rmtree(state)
        after = production()
        report["final_production"] = after
        report["production_unchanged"] = all(
            before[key] == after[key]
            for key in (
                "release",
                "database_container_id",
                "database_started_at",
                "production_data_volume",
                "api_image",
                "api_container_id",
                "api_started_at",
                "schema",
            )
        )
        report["existing_test_database_state_preserved"] = (
            inspect("agent-hub-complete-test-db")["State"]["Running"] == previous_test_running
        )
        report["cleanup"] = {
            "containers_removed": True,
            "private_test_volume_removed": True,
            "private_network_removed": True,
            "random_environment_removed": True,
            "synthetic_credentials_removed": True,
        }
        report["finished_unix"] = time.time()
        report["passed"] = bool(
            report["passed"]
            and report["production_unchanged"]
            and report["existing_test_database_state_preserved"]
        )
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        args.report.chmod(0o600)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "report": str(args.report),
                    "production_unchanged": report["production_unchanged"],
                    "cleanup": report["cleanup"],
                }
            ),
            flush=True,
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
