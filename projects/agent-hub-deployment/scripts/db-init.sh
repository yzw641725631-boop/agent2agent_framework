#!/bin/sh
set -eu
export HUB_OWNER_PASSWORD="$(cat /run/secrets/owner_password)"
export HUB_RUNTIME_PASSWORD="$(cat /run/secrets/runtime_password)"
export HUB_INDEXER_PASSWORD="$(cat /run/secrets/document_password)"
export HUB_MEMORY_PASSWORD="$(cat /run/secrets/memory_password)"
export HUB_EMBEDDING_PASSWORD="$(cat /run/secrets/embedding_password)"
export HUB_GENERATION_PASSWORD="$(cat /run/secrets/generation_password)"
psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /deployment/01_roles.sql
