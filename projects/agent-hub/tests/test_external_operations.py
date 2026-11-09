import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path


def load(name):
    file = Path(__file__).parents[1] / "deploy" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_monitor_retries_delivery_and_only_alerts_on_change_or_hourly_failure():
    module = load("external-watch")
    time = datetime(2026, 11, 9, tzinfo=UTC)
    config = {"health_url": "https://service.example/healthz", "webhook_url": "https://alerts.example/hook"}
    sent = []

    def down(url, **kwargs):
        if url == config["health_url"]:
            raise OSError("private-error-content")
        sent.append(kwargs["payload"])

    first = module.check(config, {}, down, time)
    assert first["status"] == "unavailable" and first["delivery_confirmed"]
    assert len(sent) == 1 and "private" not in str(sent)
    module.check(config, first, down, time + timedelta(minutes=1))
    assert len(sent) == 1
    module.check(config, first, down, time + timedelta(hours=1))
    assert len(sent) == 2

    def fail_delivery(url, **kwargs):
        raise OSError("secret")

    failed = module.check(config, {}, fail_delivery, time)
    assert (
        not failed["delivery_confirmed"]
        and failed["status"] == "unavailable"
        and failed["notified_status"] is None
    )
    retry = module.check(config, failed, down, time + timedelta(minutes=1))
    assert retry["delivery_confirmed"] and retry["notified_status"] == "unavailable"
    recovered = module.check(config, first, lambda url, **kwargs: {"status": "ok"}, time)
    assert recovered["status"] == "ok" and recovered["delivery_confirmed"]


def test_offsite_copy_confirms_exact_snapshot_and_checks_remote_data(monkeypatch, tmp_path):
    module = load("offsite-backup")
    snapshot = "a" * 64
    calls = []
    monkeypatch.setattr(module, "BASE", tmp_path)
    (tmp_path / "restic").mkdir()
    (tmp_path / "last-success.json").write_text(
        '{"snapshot_id":"'
        + snapshot
        + '","created_at":"'
        + datetime.now(UTC).isoformat()
        + '","full_repository_check":true}'
    )
    monkeypatch.setattr(module, "protected", lambda file: file)

    def invoke(config, args):
        calls.append(args)
        return (
            ('[{"id":"' + "b" * 64 + '","original":"' + snapshot + '"}]').encode()
            if args == ["snapshots", "--json"]
            else b""
        )

    monkeypatch.setattr(module, "invoke", invoke)
    result = module.copy_snapshot({})
    assert result["offsite_confirmed"] and result["source_snapshot_id"] == snapshot
    assert calls[0][-1] == snapshot and calls[-1] == ["check", "--read-data"]
    assert not any("forget" in call or "prune" in call or "init" in call for call in calls)
