#!/bin/sh
# Recreate a temporary database from encrypted backup and decrypt every restored artifact.
set -eu
umask 077
base=/opt/agent-hub/backups
test "$(id -u)" = 0
test ! -L "$base"
exec 8>/opt/agent-hub/database/deploy.lock
flock -s -n 8
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
tool=${HUB_CHECKPOINT_TOOL:-/opt/agent-hub/application/current/deploy/database-checkpoint.py}
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
python3 - "$stage/data/database" "$stage/runtime.json" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
directory=Path(sys.argv[1]);manifest=json.loads((directory/'manifest.json').read_text())
assert manifest['database']=='agent_hub' and manifest['artifacts_immutable'] is True
if manifest['schema']==1:
    # Explicit compatibility for backups made before versioned recovery manifests.
    runtime={'image':'agent-hub:0.14.0','version':'0.14.0','database_schema':'0017','inventory_verified':False}
else:
    assert manifest['schema']==2 and manifest['consistent_database_snapshot'] is True
    assert re.fullmatch(r'sha256:[a-f0-9]{64}',manifest['application_image'])
    assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',manifest['application_version'])
    assert re.fullmatch(r'[0-9]{4}',manifest['database_schema'])
    checksums=manifest['database_files_sha256']
    assert set(checksums)=={'database.dump','globals.sql','inventory.json'}
    for name,expected in checksums.items():
        digest=hashlib.sha256()
        with (directory/name).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
        assert digest.hexdigest()==expected
    runtime={'image':manifest['application_image'],'version':manifest['application_version'],
             'database_schema':manifest['database_schema'],'inventory_verified':True}
with open(sys.argv[2],'x') as stream:json.dump(runtime,stream)
PY
image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image"])' "$stage/runtime.json")
version=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$stage/runtime.json")
schema=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["database_schema"])' "$stage/runtime.json")
docker image inspect "$image" >/dev/null
test ! -L "$stage/data/work-archives"
python3 - "$stage/data/semantic-model" <<'PY'
import hashlib,json,sys
from pathlib import Path
directory=Path(sys.argv[1])
manifest=json.loads((directory/'manifest.json').read_text())
assert manifest['model_id']=='bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1'
for name,digest in manifest['files'].items():
    assert Path(name).name==name
    assert hashlib.sha256((directory/name).read_bytes()).hexdigest()==digest
PY
chown -R --no-dereference 10001:10001 "$stage/data/work-archives"
docker exec agent-hub-postgres psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$target\" TEMPLATE template0" >/dev/null
docker exec -i agent-hub-postgres pg_restore -U postgres -d "$target" --exit-on-error <"$stage/data/database/database.dump"
if test -s "$stage/data/database/inventory.json"; then
  python3 "$tool" verify --container agent-hub-postgres --database "$target" \
    --baseline "$stage/data/database/inventory.json" --output "$stage/database-verification.json" >/dev/null
fi
# Only the restored artifact directory is exposed. SMTP and public serving are disabled.
docker run --rm --network agent-hub-database --read-only --tmpfs /tmp --memory 384m --cpus 0.5 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --env-file "$stage/data/config/database.env" --env-file "$stage/data/config/application.env" \
  -e HUB_ENVIRONMENT=test -e HUB_MAIL_MODE=memory -e HUB_RESTORE_DATABASE="$target" \
  -e HUB_RESTORE_SNAPSHOT="$snapshot" \
  -e HUB_RESTORE_VERSION="$version" -e HUB_RESTORE_SCHEMA="$schema" \
  -e HUB_PUBLIC_URL=http://127.0.0.1:8765 -e HUB_ARTIFACT_DIRECTORY=/restored-archives \
  -v "$stage/data/work-archives:/restored-archives:ro" --entrypoint python "$image" -c '
import datetime,json,os,re
from urllib.parse import quote
from sqlalchemy import text
from agent_hub.config import Settings
from agent_hub.db import session_factory
from agent_hub.archive_maintenance import audit
from agent_hub.memory_maintenance import audit as audit_memory
from agent_hub import __version__
assert __version__==os.environ["HUB_RESTORE_VERSION"]
target=os.environ["HUB_RESTORE_DATABASE"]
assert re.fullmatch(r"hub_restore_[0-9]{8}T[0-9]{6}Z",target)
url="postgresql+psycopg://hub_owner:"+quote(os.environ["HUB_OWNER_PASSWORD"],safe="")+"@postgres:5432/"+target
cfg=Settings(database_url=url)
factory=session_factory(cfg)
with factory() as db:
    assert db.scalar(text("SELECT current_database()"))==target
    schema=db.scalar(text("SELECT version_num FROM alembic_version"))
    assert schema==os.environ["HUB_RESTORE_SCHEMA"]
    assert db.scalar(text("SELECT count(*) FROM task_runs r JOIN tasks t ON r.task_id=t.id WHERE r.state=:state AND r.role<>t.execution_role"),{"state":"active"})==0
    assert db.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname=:name"),{"name":"one_active_primary_run"})==1
    assert db.scalar(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname=:name"),{"name":"task_dependencies"})
    assert db.scalar(text("SELECT count(*) FROM task_dependencies d LEFT JOIN tasks a ON a.id=d.task_id AND a.project_id=d.project_id LEFT JOIN tasks b ON b.id=d.depends_on_id AND b.project_id=d.project_id WHERE a.id IS NULL OR b.id IS NULL"))==0
result=audit(cfg,factory)
assert result["healthy"] and result["verified"]==result["recorded"]
memory=audit_memory(factory)
assert memory["healthy"]
print(json.dumps({"restored_database":target,"snapshot_id":os.environ["HUB_RESTORE_SNAPSHOT"],
                  "database_schema":schema,"role_bindings_verified":True,
                  "verified_archives":result["verified"],"memory":memory,"passed":True,
                  "created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}))
' >"$stage/result.json"
python3 - "$stage/result.json" "$stage/runtime.json" "$stage/database-verification.json" <<'PY'
import json,sys
result=json.load(open(sys.argv[1]));runtime=json.load(open(sys.argv[2]))
result['application_version']=runtime['version']
result['database_inventory_verified']=runtime['inventory_verified']
if runtime['inventory_verified']:
    verification=json.load(open(sys.argv[3]));assert verification['passed'] is True
    result['verified_tables']=verification['verified_tables']
    result['verified_rows']=verification['verified_rows']
    result['structure_verified']=verification['structure_verified']
with open(sys.argv[1],'w') as stream:json.dump(result,stream)
PY
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
