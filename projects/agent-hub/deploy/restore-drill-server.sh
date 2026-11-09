#!/bin/sh
# Recreate a temporary database from encrypted backup and decrypt every restored artifact.
set -eu
umask 077
base=/opt/agent-hub/backups
test "$(id -u)" = 0
test ! -L "$base"
exec 9>"$base/backup.lock"
flock -n 9
snapshot=$(python3 - "$base/last-success.json" <<'PY'
import json,re,sys
result=json.load(open(sys.argv[1]))
snapshot=result['snapshot_id']
assert result['full_repository_check'] is True and re.match(r'^[a-f0-9]{64}$',snapshot)
print(snapshot)
PY
)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="hub_restore_$stamp"
case "$target" in hub_restore_[0-9]*Z) ;; *) exit 1 ;; esac
stage="$base/drills/$stamp"
test ! -e "$stage"
mkdir -p "$stage"
docker run --rm --network none --read-only --memory 384m --cpus 0.5 --pids-limit 64 \
  --tmpfs /tmp:rw,nosuid,noexec,size=64m -e GOMEMLIMIT=320MiB \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --security-opt no-new-privileges:true \
  -v "$base/restic:/repository:ro" -v "$base/restic-password:/run/secrets/password:ro" \
  -v "$stage:/restore" -e RESTIC_REPOSITORY=/repository -e RESTIC_PASSWORD_FILE=/run/secrets/password \
  restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 \
  --no-cache --no-lock restore "$snapshot" --host agent-hub-47 --tag agent-hub-production --target /restore >/dev/null
test -s "$stage/data/database/database.dump"
python3 - "$stage/data/database/manifest.json" <<'PY'
import json,sys
manifest=json.load(open(sys.argv[1]))
assert manifest['schema']==1 and manifest['database']=='agent_hub' and manifest['artifacts_immutable'] is True
PY
test ! -L "$stage/data/work-archives"
chown -R --no-dereference 10001:10001 "$stage/data/work-archives"
docker exec agent-hub-postgres psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$target\" TEMPLATE template0" >/dev/null
docker exec -i agent-hub-postgres pg_restore -U postgres -d "$target" --exit-on-error <"$stage/data/database/database.dump"
# Only the restored artifact directory is exposed. SMTP and public serving are disabled.
docker run --rm --network agent-hub-database --read-only --tmpfs /tmp --memory 384m --cpus 0.5 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --env-file "$stage/data/config/database.env" --env-file "$stage/data/config/application.env" \
  -e HUB_ENVIRONMENT=test -e HUB_MAIL_MODE=memory -e HUB_RESTORE_DATABASE="$target" \
  -e HUB_RESTORE_SNAPSHOT="$snapshot" \
  -e HUB_PUBLIC_URL=http://127.0.0.1:8765 -e HUB_ARTIFACT_DIRECTORY=/restored-archives \
  -v "$stage/data/work-archives:/restored-archives:ro" --entrypoint python agent-hub:0.8.0 -c '
import datetime,json,os,re
from urllib.parse import quote
from sqlalchemy import text
from agent_hub.config import Settings
from agent_hub.db import session_factory
from agent_hub.archive_maintenance import audit
target=os.environ["HUB_RESTORE_DATABASE"]
assert re.fullmatch(r"hub_restore_[0-9]{8}T[0-9]{6}Z",target)
url="postgresql+psycopg://hub_owner:"+quote(os.environ["HUB_OWNER_PASSWORD"],safe="")+"@postgres:5432/"+target
cfg=Settings(database_url=url)
factory=session_factory(cfg)
with factory() as db:
    assert db.scalar(text("SELECT current_database()"))==target
result=audit(cfg,factory)
assert result["healthy"] and result["verified"]==result["recorded"]
print(json.dumps({"restored_database":target,"snapshot_id":os.environ["HUB_RESTORE_SNAPSHOT"],
                  "verified_archives":result["verified"],"passed":True,
                  "created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}))
' >"$stage/result.json"
cat "$stage/result.json"
docker exec agent-hub-postgres psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE \"$target\"" >/dev/null
cp "$stage/result.json" "$base/last-restore-drill.json"
python3 - "$stage" <<'PY'
from pathlib import Path
import shutil,sys
root=Path('/opt/agent-hub/backups/drills').resolve()
target=Path(sys.argv[1]).resolve()
assert target.parent==root and target.name.endswith('Z') and not Path(sys.argv[1]).is_symlink()
shutil.rmtree(target)
PY
