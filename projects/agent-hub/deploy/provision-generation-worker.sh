#!/bin/sh
# Run on the authorized server as the database operator before migration 0025.
set -eu
umask 077
test "$(id -u)" = 0
db_env=/opt/agent-hub/database/.env
test -f "$db_env" && test ! -L "$db_env"
if ! grep -q '^HUB_GENERATION_PASSWORD=' "$db_env"; then
  printf 'HUB_GENERATION_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> "$db_env"
fi
chmod 600 "$db_env"
. "$db_env"
docker exec -i agent-hub-postgres psql -X -q -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -v worker_password="$HUB_GENERATION_PASSWORD" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_generation') THEN
    CREATE ROLE hub_generation NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_generation_worker') THEN
    CREATE ROLE hub_generation_worker LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
SELECT format('ALTER ROLE hub_generation_worker PASSWORD %L', :'worker_password') \gexec
GRANT hub_runtime TO hub_generation;
GRANT hub_generation TO hub_owner,hub_generation_worker;
SQL
printf 'Separate generation worker role provisioned. No model provider is enabled.\n'
