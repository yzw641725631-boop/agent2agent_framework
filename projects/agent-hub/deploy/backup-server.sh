#!/bin/sh
# Run as the server operator. Immutable artifacts are copied after the consistent DB dump.
set -eu
umask 077
base=/opt/agent-hub/backups
test "$(id -u)" = 0
test ! -L "$base"
mkdir -p "$base/staging"
chmod 0700 "$base" "$base/staging"
exec 8>/opt/agent-hub/database/deploy.lock
flock -s -n 8
exec 9>"$base/backup.lock"
flock -n 9
test -s "$base/restic-password"
stage=$(mktemp -d "$base/staging/backup-XXXXXXXX")
case "$stage" in "$base"/staging/backup-*) ;; *) exit 1 ;; esac
checkpoint="$stage/checkpoint"
tool=${HUB_CHECKPOINT_TOOL:-/opt/agent-hub/application/current/deploy/database-checkpoint.py}
cleanup() {
  rm -f "$checkpoint/database.dump" "$checkpoint/inventory.json" "$checkpoint/globals.sql" "$checkpoint/manifest.json" "$stage/restic.json"
  if test -d "$checkpoint"; then rmdir "$checkpoint"; fi
  rmdir "$stage"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
python3 "$tool" capture --container agent-hub-postgres --database agent_hub --output "$checkpoint" >/dev/null
docker exec -i agent-hub-postgres pg_restore --list <"$checkpoint/database.dump" >/dev/null
docker exec agent-hub-postgres pg_dumpall -U postgres --globals-only >"$checkpoint/globals.sql"
image=$(docker inspect --format '{{.Image}}' agent-hub-app-api-1)
version=$(docker exec agent-hub-app-api-1 python -c 'from agent_hub import __version__; print(__version__)')
python3 - "$checkpoint" "$image" "$version" <<'PY'
import datetime,hashlib,json,re,sys
from pathlib import Path
directory=Path(sys.argv[1])
assert re.fullmatch(r'sha256:[a-f0-9]{64}',sys.argv[2])
assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',sys.argv[3])
inventory=json.loads((directory/'inventory.json').read_text())
checksums={}
for name in ('database.dump','globals.sql','inventory.json'):
    digest=hashlib.sha256()
    with (directory/name).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
    checksums[name]=digest.hexdigest()
with (directory/'manifest.json').open('x') as output:
    json.dump({'schema':2,'database':'agent_hub','artifacts_immutable':True,
               'consistent_database_snapshot':True,'database_schema':inventory['database_schema'],
               'application_image':sys.argv[2],'application_version':sys.argv[3],
               'database_files_sha256':checksums,
               'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'offsite_confirmed':False},output)
PY
restic() {
  docker run --rm --network none --read-only --memory 384m --cpus 0.5 --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,noexec,size=64m -e GOMEMLIMIT=320MiB \
    --cap-drop ALL --cap-add DAC_READ_SEARCH --security-opt no-new-privileges:true \
    -v "$base/restic:/repository" -v "$base/restic-password:/run/secrets/password:ro" \
    -v "$checkpoint:/data/database:ro" \
    -v /opt/agent-hub/artifacts/work:/data/work-archives:ro \
    -v /opt/agent-hub/semantic/model-fp32:/data/semantic-model:ro \
    -v /opt/agent-hub/application/.env:/data/config/application.env:ro \
    -v /opt/agent-hub/database/.env:/data/config/database.env:ro \
    -e RESTIC_REPOSITORY=/repository -e RESTIC_PASSWORD_FILE=/run/secrets/password \
    restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 \
    --no-cache "$@"
}
restic backup /data --exclude /data/database/restic.json --host agent-hub-47 --tag agent-hub-production --json >"$stage/restic.json"
restic check --read-data >/dev/null
python3 - "$stage/restic.json" "$base/last-success.json" <<'PY'
import datetime,json,os,sys
rows=[json.loads(line) for line in open(sys.argv[1])]
summary=next(row for row in rows if row.get('message_type')=='summary')
result={'schema':1,'snapshot_id':summary['snapshot_id'],
        'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'full_repository_check':True,'offsite_confirmed':False}
with open(sys.argv[2]+'.next','w') as output:
    json.dump(result,output)
os.replace(sys.argv[2]+'.next',sys.argv[2])
print(json.dumps(result))
PY
