#!/bin/sh
# Existing authorized server only. Run before migration 0005; never send this .env to a device.
set -eu
umask 077
db_env=/opt/agent-hub/database/.env
if ! grep -q '^HUB_INDEXER_PASSWORD=' "$db_env"; then
  printf 'HUB_INDEXER_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> "$db_env"
fi
chmod 600 "$db_env"
. "$db_env"
docker exec -i agent-hub-postgres psql -X -q -U postgres -d postgres -v ON_ERROR_STOP=1 -v worker_password="$HUB_INDEXER_PASSWORD" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hub_indexer') THEN CREATE ROLE hub_indexer NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hub_document_worker') THEN
    CREATE ROLE hub_document_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
SELECT format('ALTER ROLE hub_document_worker PASSWORD %L', :'worker_password') \gexec
GRANT hub_indexer TO hub_owner, hub_document_worker;
SQL
