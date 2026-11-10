#!/bin/sh
# Operator-only 0.16.0 -> 0.17.0 application upgrade. Schema stays 0026.
set -eu
umask 077
test "$(id -u)" = 0
app=/opt/agent-hub/application
database=/opt/agent-hub/database
release="$app/releases/0.17.0"
previous="$app/releases/0.16.0"
script_directory=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
test "$script_directory" = "$release/deploy"
test ! -L "$release"
test "$(readlink -f "$app/current")" = "$previous"
exec 8>"$database/deploy.lock"
flock -n 8
exec 9>/opt/agent-hub/backups/backup.lock
flock -n 9
python3 - "$release" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
root=Path(sys.argv[1]);record=json.loads((root/'release-validation.json').read_text())
assert record['schema']==1 and record['passed'] is True
assert record['version']=='0.17.0' and record['database_schema']=='0026'
assert record['backend_tests_passed']>=348 and record['migration_unchanged'] is True
assert record['reference_tokenizer_verified'] is True and record['dependencies_verified'] is True
assert re.fullmatch(r'sha256:[a-f0-9]{64}',record['image_id'])
assert re.fullmatch(r'sha256:[a-f0-9]{64}',record['rollback_image_id'])
files=record['verified_files'];assert 20<=len(files)<=10000
for name,expected in files.items():
    path=root/name
    assert not Path(name).is_absolute() and '..' not in Path(name).parts
    assert path.is_file() and not path.is_symlink() and root.resolve() in path.resolve().parents
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected
for folder in ('src','migrations','deploy','tests','scripts','examples'):
    for path in (root/folder).rglob('*'):
        assert not path.is_symlink()
        if path.is_file(): assert str(path.relative_to(root)) in files
PY
expected_image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image_id"])' "$release/release-validation.json")
expected_rollback=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["rollback_image_id"])' "$release/release-validation.json")
test "$(docker image inspect agent-hub:0.17.0 --format '{{.Id}}')" = "$expected_image"
test "$(docker image inspect agent-hub:0.16.0 --format '{{.Id}}')" = "$expected_rollback"
test "$(docker exec agent-hub-app-api-1 python -c 'from agent_hub import __version__;print(__version__)')" = 0.16.0
test "$(docker inspect agent-hub-postgres --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql"}}{{.Name}}{{end}}{{end}}')" = agent-hub-postgres-data
revision() { docker exec agent-hub-postgres psql -X -qAt -U postgres -d agent_hub -c 'SELECT version_num FROM alembic_version'; }
test "$(revision)" = 0026
diff -qr "$previous/migrations" "$release/migrations" >/dev/null
cmp "$previous/deploy/02_schema.sql" "$release/deploy/02_schema.sql"
python3 - <<'PY'
import datetime,json
from pathlib import Path
root=Path('/opt/agent-hub/backups')
backup=json.loads((root/'last-success.json').read_text());restore=json.loads((root/'last-restore-drill.json').read_text())
assert backup['full_repository_check'] is True and restore['passed'] is True
assert restore['snapshot_id']==backup['snapshot_id'] and restore['database_schema']=='0026'
assert restore['database_inventory_verified'] is True and restore['structure_verified'] is True
assert restore['application_version']=='0.16.0'
for record in (backup,restore):
    timestamp=datetime.datetime.strptime(record['created_at'][:19],'%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
    age=(datetime.datetime.now(datetime.timezone.utc)-timestamp).total_seconds()
    assert -300<=age<=21600
PY
timers='agent-hub-backup.timer agent-hub-restore-drill.timer agent-hub-archive-audit.timer agent-hub-ops-check.timer'
for timer in $timers; do systemctl is-active "$timer" >/dev/null; done
for task in backup restore-drill archive-audit ops-check; do
  test "$(systemctl show "agent-hub-$task.service" -p ActiveState)" = ActiveState=inactive
done
stamp=$(date -u +%Y%m%dT%H%M%SZ)
state="$release/operator-$stamp"
mkdir -m 0700 "$state"
writers_stopped=0
committed=0
compose() { docker compose --env-file "$database/.env" --env-file "$app/.env" -f "$release/deploy/compose.server-app.yaml" "$@"; }
old_compose() { docker compose --env-file "$database/.env" --env-file "$app/.env" -f "$previous/deploy/compose.server-app.yaml" "$@"; }
finish() {
  result=$?
  trap - EXIT HUP INT TERM
  if test "$result" != 0 && test "$writers_stopped" = 1 && test "$committed" = 0; then
    compose stop api document-worker memory-worker embedding-worker generation-worker >"$state/rollback-stop.log" 2>&1 || true
    if old_compose up -d --no-build --no-deps api document-worker memory-worker embedding-worker generation-worker >"$state/rollback-start.log" 2>&1; then
      docker exec agent-hub-edge nginx -s reload >/dev/null 2>&1 || true
      printf 'Upgrade failed; previous application restarted on unchanged schema 0026. Data retained.\n' >&2
    else
      printf 'Application rollback failed; operator action required. Database and backups retained.\n' >&2
    fi
  fi
  for timer in $timers; do if ! systemctl start "$timer"; then result=1; fi; done
  exit "$result"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
systemctl stop $timers
writers_stopped=1
old_compose stop api document-worker memory-worker embedding-worker generation-worker >"$state/stop.log" 2>&1
python3 "$release/deploy/database-checkpoint.py" capture --output "$state/before" >"$state/capture.json"
# No migration is run. Verify the complete inventory before restarting writers.
python3 "$release/deploy/database-checkpoint.py" verify --mode exact \
  --baseline "$state/before/inventory.json" --output "$state/preservation.json" >/dev/null
compose up -d --no-build --no-deps api document-worker memory-worker embedding-worker generation-worker >"$state/start.log" 2>&1
ready=0
for attempt in $(seq 1 60); do
  if docker exec agent-hub-app-api-1 python -c 'import json,urllib.request;v=json.load(urllib.request.urlopen("http://127.0.0.1:8765/healthz",timeout=2));assert v["version"]=="0.17.0" and v["database_schema"]=="0026"' >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
test "$ready" = 1
for service in api document-worker memory-worker embedding-worker generation-worker; do
  test "$(docker inspect "agent-hub-app-$service-1" --format '{{.State.Running}}')" = true
  docker exec "agent-hub-app-$service-1" python -c 'from agent_hub import __version__;assert __version__=="0.17.0"'
done
docker exec agent-hub-app-api-1 python -c 'from agent_hub.memory_budget import Counter;assert Counter().method=="cl100k_base"'
test "$(revision)" = 0026
test "$(docker inspect agent-hub-app-embedding-1 --format '{{.State.Health.Status}}')" = healthy
docker exec agent-hub-edge nginx -t >"$state/nginx-check.log" 2>&1
docker exec agent-hub-edge nginx -s reload
curl -fsS --retry 3 --max-time 10 https://47.109.23.86:18180/healthz >"$state/public-health.json"
curl -fsS --max-time 10 https://47.109.23.86:18180/assets/app.js >"$state/public-app.js"
cmp "$state/public-app.js" "$release/src/agent_hub/static/app.js"
test "$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' https://47.109.23.86:18180/api/me)" = 401
python3 - "$state/public-health.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]));assert value['status']=='ok' and value['version']=='0.17.0' and value['database_schema']=='0026'
PY
ln -s "$release" "$app/.current-0170-$stamp"
mv -Tf "$app/.current-0170-$stamp" "$app/current"
committed=1
python3 - "$state" "$expected_image" <<'PY'
import datetime,json,sys
from pathlib import Path
state=Path(sys.argv[1]);data=json.loads((state/'preservation.json').read_text())
data.update(schema=1,passed=True,application_version='0.17.0',database_schema='0026',
            previous_application_version='0.16.0',previous_database_schema='0026',image_id=sys.argv[2],
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            production_volume_preserved=True,migration_performed=False,business_pilot_performed=False)
with (state.parent/'production-upgrade.json').open('x') as stream:json.dump(data,stream)
print(json.dumps(data))
PY
