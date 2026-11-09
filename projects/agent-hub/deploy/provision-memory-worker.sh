#!/bin/sh
# Existing authorized server only. Run after the vector image is installed, before 0012.
set -eu
umask 077
db_env=/opt/agent-hub/database/.env
if ! grep -q '^HUB_MEMORY_PASSWORD=' "$db_env"; then
  printf 'HUB_MEMORY_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> "$db_env"
fi
chmod 600 "$db_env"
. "$db_env"
docker exec -i agent-hub-postgres psql -X -q -U postgres -d postgres -v ON_ERROR_STOP=1 -v worker_password="$HUB_MEMORY_PASSWORD" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_memory') THEN CREATE ROLE hub_memory NOLOGIN; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_memory_worker') THEN
    CREATE ROLE hub_memory_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
  END IF;
END $$;
SELECT format('ALTER ROLE hub_memory_worker PASSWORD %L', :'worker_password') \gexec
GRANT hub_memory TO hub_owner,hub_memory_worker;
SQL
docker exec agent-hub-postgres psql -X -q -U postgres -d agent_hub -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector'
