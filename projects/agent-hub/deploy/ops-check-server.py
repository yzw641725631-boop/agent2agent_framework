#!/usr/bin/env python3
# ruff: noqa: UP017, UP021, UP022
"""Local operator monitor. No credentials or business records enter the report.

Compatible with the system Python used by systemd on the current host (3.6+).
An external monitor is still needed to detect host/network failure and deliver alerts.
"""
import datetime
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

BASE = Path('/opt/agent-hub/backups')
ORIGIN = 'https://47.109.23.86:18180'
UTC = datetime.timezone.utc


def command(arguments, timeout=12):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout)
    if len(result.stdout) > 65536:
        raise ValueError('Oversized operator output')
    return result.returncode, result.stdout


def record(file):
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError('Invalid operator record')
    with file.open() as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError('Invalid operator record')
    return value


def age_hours(value, time):
    # fromisoformat is unavailable in the oldest supported host Python.
    instant = datetime.datetime.strptime(value['created_at'][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)
    if not (value['created_at'].endswith('+00:00') or value['created_at'].endswith('Z')):
        raise ValueError('Expected UTC timestamp')
    hours = (time - instant).total_seconds() / 3600
    if hours < -0.1:
        raise ValueError('Operator record is from the future')
    return max(0, hours)


def collect(base=BASE, time=None):
    time = time or datetime.datetime.now(UTC)
    checks = []

    def check(name, action):
        try:
            result = action()
            checks.append({'id': name, 'state': 'ok', **(result or {})})
        except Exception:
            # Commands, database errors and files may contain sensitive diagnostics.
            checks.append({'id': name, 'state': 'error'})

    def container(name):
        code, output = command(['docker', 'inspect', '--format', '{{json .State}}', name])
        if code:
            raise ValueError()
        state = json.loads(output)
        if not state['Running'] or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise ValueError()

    for name in ['agent-hub-postgres', 'agent-hub-app-api-1', 'agent-hub-app-document-worker-1', 'agent-hub-edge']:
        check('container:' + name, lambda name=name: container(name))

    def database():
        code, output = command(['docker', 'exec', 'agent-hub-postgres', 'psql', '-X', '-U',
                                'postgres', '-d', 'agent_hub', '-Atc', 'SELECT 1'])
        if code or output.strip() != '1':
            raise ValueError()
    check('database:query', database)

    def public_health():
        with urlopen(ORIGIN + '/healthz', timeout=10) as response:
            value = json.loads(response.read(4097))
        if value.get('status') != 'ok':
            raise ValueError()
        return {'version': value.get('version')}
    check('public:https', public_health)

    def feed():
        code, output = command(['docker', 'exec', 'agent-hub-app-api-1', 'agent-hub', 'check-desktop-feed'], timeout=20)
        if code not in (0, 2):
            raise ValueError()
        value = json.loads(output)
        # This CLI checks TLS, the pinned release signature and expiry.
        return {'state': 'warning' if code == 2 else 'ok',
                'version': value['version'], 'remaining_hours': value['remaining_hours']}
    check('desktop:signed-feed', feed)

    def disk():
        space = os.statvfs(str(base))
        free = space.f_bavail * space.f_frsize
        ratio = space.f_bavail / max(1, space.f_blocks)
        if free < 4 * 1024 ** 3 or ratio < 0.05:
            raise ValueError()
        return {'state': 'warning' if ratio < 0.15 else 'ok', 'free_bytes': free}
    check('disk:capacity', disk)

    def maintenance(name, file, max_hours, success_field):
        for suffix in ['service', 'timer']:
            code, output = command(['systemctl', 'show', name + '.' + suffix,
                                    '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Result'])
            values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
            if code or values.get('LoadState') != 'loaded' or values.get('ActiveState') == 'failed':
                raise ValueError()
            if suffix == 'timer' and values.get('ActiveState') != 'active':
                raise ValueError()
        value = record(base / file)
        if value.get(success_field) is not True:
            raise ValueError()
        age = age_hours(value, time)
        if age > max_hours:
            raise ValueError()
        return {'age_hours': round(age, 2), 'maximum_hours': max_hours}

    for name, file, maximum, success in [
        ('agent-hub-backup', 'last-success.json', 36, 'full_repository_check'),
        ('agent-hub-archive-audit', 'last-archive-audit.json', 36, 'healthy'),
        ('agent-hub-restore-drill', 'last-restore-drill.json', 45 * 24, 'passed'),
    ]:
        check(name, lambda name=name, file=file, maximum=maximum, success=success:
              maintenance(name, file, maximum, success))

    # Do not turn a one-time operator download into a claim of ongoing redundancy.
    checks.append({'id': 'offsite:continuous-copy', 'state': 'warning', 'configured': False})
    checks.append({'id': 'alerts:external-delivery', 'state': 'warning', 'configured': False})
    return {'schema': 1, 'created_at': time.isoformat(), 'checks': checks,
            'healthy': not any(row['state'] == 'error' for row in checks),
            'has_warnings': any(row['state'] == 'warning' for row in checks),
            'scope': 'local-operator-checks-only'}


def main():
    if os.geteuid() != 0 or any(part.is_symlink() for part in [BASE, *BASE.parents]):
        raise RuntimeError('Use the real operator backup directory as root')
    os.umask(0o077)
    BASE.mkdir(exist_ok=True, mode=0o700)
    result = collect()
    descriptor, name = tempfile.mkstemp(prefix='ops-check-', dir=str(BASE))
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(result, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, str(BASE / 'last-ops-check.json'))
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(json.dumps(result))
    return 0 if result['healthy'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
