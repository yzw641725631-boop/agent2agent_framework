"""Operational failures must remain visible even after an older successful backup."""
import datetime
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "hub_ops_monitor", Path(__file__).resolve().parents[1] / "deploy/ops-check-server.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
TIME = datetime.datetime(2026, 11, 9, 2, tzinfo=datetime.UTC)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    records = {
        'last-success.json': {'full_repository_check': True},
        'last-archive-audit.json': {'healthy': True},
        'last-restore-drill.json': {'passed': True},
    }
    for name, value in records.items():
        (tmp_path / name).write_text(json.dumps({'created_at': TIME.isoformat(), **value}))
    state = {'failed_unit': None, 'feed_exit': 0, 'feed_hours': 720}

    def command(arguments, timeout=12):
        if arguments[0] == 'systemctl':
            unit_state = 'failed' if arguments[2] == state['failed_unit'] else 'active'
            return 0, f'LoadState=loaded\nActiveState={unit_state}\nResult=success\n'
        if arguments[1] == 'inspect':
            return 0, json.dumps({'Running': True, 'Health': {'Status': 'healthy'}})
        if arguments[-1] == 'SELECT 1':
            return 0, '1\n'
        return state['feed_exit'], json.dumps({'version': '0.9.0', 'remaining_hours': state['feed_hours']})

    @contextmanager
    def urlopen(url, timeout):
        yield SimpleNamespace(read=lambda limit: b'{"status":"ok","version":"0.7.0"}')

    monkeypatch.setattr(monitor, 'command', command)
    monkeypatch.setattr(monitor, 'urlopen', urlopen)
    monkeypatch.setattr(monitor.os, 'statvfs', lambda path: SimpleNamespace(
        f_bavail=20 * 1024 ** 3, f_frsize=1, f_blocks=80 * 1024 ** 3
    ), raising=False)
    return tmp_path, state


def result(environment):
    return monitor.collect(environment[0], TIME)


def states(report):
    return {row['id']: row['state'] for row in report['checks']}


def test_local_success_keeps_unconfigured_offsite_and_alert_warnings(environment):
    report = result(environment)
    assert report['healthy'] is True
    assert report['has_warnings'] is True
    assert states(report)['offsite:continuous-copy'] == 'warning'
    assert states(report)['alerts:external-delivery'] == 'warning'


@pytest.mark.parametrize('failure', ['missing', 'expired', 'future', 'incomplete', 'symlink', 'oversized'])
def test_invalid_backup_record_is_an_error_even_with_green_units(environment, failure):
    file = environment[0] / 'last-success.json'
    if failure in {'missing', 'symlink'}:
        file.unlink()
        if failure == 'symlink':
            try:
                file.symlink_to(environment[0] / 'last-restore-drill.json')
            except OSError:
                pytest.skip('Host does not allow symlink creation')
    else:
        value = json.loads(file.read_text())
        if failure == 'expired':
            value['created_at'] = (TIME - datetime.timedelta(hours=37)).isoformat()
        elif failure == 'future':
            value['created_at'] = (TIME + datetime.timedelta(hours=1)).isoformat()
        elif failure == 'incomplete':
            value['full_repository_check'] = False
        else:
            value['untrusted'] = 'x' * 65536
        file.write_text(json.dumps(value))
    report = result(environment)
    assert report['healthy'] is False
    assert states(report)['agent-hub-backup'] == 'error'


def test_failed_service_is_an_error_with_a_recent_success_file(environment):
    environment[1]['failed_unit'] = 'agent-hub-backup.service'
    assert states(result(environment))['agent-hub-backup'] == 'error'


def test_overdue_recovery_drill_is_an_error(environment):
    file = environment[0] / 'last-restore-drill.json'
    file.write_text(json.dumps({'passed': True, 'created_at': (TIME - datetime.timedelta(days=46)).isoformat()}))
    assert states(result(environment))['agent-hub-restore-drill'] == 'error'


def test_near_expiry_signed_feed_warns_and_invalid_signature_fails(environment):
    environment[1].update(feed_exit=2, feed_hours=48)
    report = result(environment)
    assert report['healthy'] is True
    assert states(report)['desktop:signed-feed'] == 'warning'
    environment[1]['feed_exit'] = 1
    assert states(result(environment))['desktop:signed-feed'] == 'error'


def test_low_disk_space_is_an_error(environment, monkeypatch):
    monkeypatch.setattr(monitor.os, 'statvfs', lambda path: SimpleNamespace(
        f_bavail=3 * 1024 ** 3, f_frsize=1, f_blocks=80 * 1024 ** 3
    ))
    assert states(result(environment))['disk:capacity'] == 'error'


def test_command_error_text_cannot_leak_into_report(environment, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError('private-command-diagnostic-do-not-publish')
    monkeypatch.setattr(monitor, 'command', failure)
    report = result(environment)
    assert report['healthy'] is False
    assert 'private-command-diagnostic-do-not-publish' not in json.dumps(report)
