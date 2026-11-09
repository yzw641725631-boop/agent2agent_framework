#!/bin/sh
# Existing authorized server only; run before schema 0016.
set -eu
umask 077
db_env=/opt/agent-hub/database/.env
if ! grep -q '^HUB_EMBEDDING_PASSWORD=' "$db_env"; then
  printf 'HUB_EMBEDDING_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> "$db_env"
fi
chmod 600 "$db_env"
. "$db_env"
docker exec -i agent-hub-postgres psql -X -q -U postgres -d postgres -v ON_ERROR_STOP=1 -v worker_password="$HUB_EMBEDDING_PASSWORD" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_embedding') THEN CREATE ROLE hub_embedding NOLOGIN; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hub_embedding_worker') THEN
    CREATE ROLE hub_embedding_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
  END IF;
END $$;
SELECT format('ALTER ROLE hub_embedding_worker PASSWORD %L', :'worker_password') \gexec
GRANT hub_embedding TO hub_owner,hub_embedding_worker;
SQL
