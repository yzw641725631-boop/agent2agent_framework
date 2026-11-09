#!/bin/sh
# Persist the most recent attempt, including a valid integrity failure report.
set -eu
umask 077
base=/opt/agent-hub/backups
test "$(id -u)" = 0
test ! -L "$base"
mkdir -p "$base"
result=$(mktemp "$base/archive-audit-XXXXXXXX")
trap 'rm -f "$result"' EXIT
trap 'exit 1' HUP INT TERM
set +e
docker compose --env-file /opt/agent-hub/database/.env --env-file /opt/agent-hub/application/.env \
  -f /opt/agent-hub/application/current/deploy/compose.server-app.yaml \
  run --rm --no-deps archive-maintenance >"$result"
status=$?
set -e
python3 - "$result" "$base/last-archive-audit.json" <<'PY'
import datetime,json,os,sys
with open(sys.argv[1]) as source:
    result=json.load(source)
assert result['schema']==1 and type(result['healthy']) is bool
result['created_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
temporary=sys.argv[2]+'.next'
with open(temporary,'w') as output:
    json.dump(result,output)
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary,sys.argv[2])
print(json.dumps(result))
PY
exit "$status"
