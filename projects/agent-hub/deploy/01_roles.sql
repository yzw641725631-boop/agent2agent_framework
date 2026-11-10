\getenv owner_password HUB_OWNER_PASSWORD
\getenv runtime_password HUB_RUNTIME_PASSWORD
\getenv indexer_password HUB_INDEXER_PASSWORD
\getenv memory_password HUB_MEMORY_PASSWORD
\getenv database_name POSTGRES_DB
CREATE ROLE hub_owner LOGIN PASSWORD :'owner_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
CREATE ROLE hub_runtime LOGIN PASSWORD :'runtime_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
CREATE ROLE hub_indexer NOLOGIN;
CREATE ROLE hub_document_worker LOGIN PASSWORD :'indexer_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
GRANT hub_indexer TO hub_owner, hub_document_worker;
CREATE ROLE hub_memory NOLOGIN;
CREATE ROLE hub_memory_worker LOGIN PASSWORD :'memory_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
GRANT hub_memory TO hub_owner, hub_memory_worker;
\getenv embedding_password HUB_EMBEDDING_PASSWORD
CREATE ROLE hub_embedding NOLOGIN;
CREATE ROLE hub_embedding_worker LOGIN PASSWORD :'embedding_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
GRANT hub_embedding TO hub_owner, hub_embedding_worker;
\getenv generation_password HUB_GENERATION_PASSWORD
CREATE ROLE hub_generation NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE hub_generation_worker LOGIN PASSWORD :'generation_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
GRANT hub_runtime TO hub_generation;
GRANT hub_generation TO hub_owner, hub_generation_worker;
ALTER DATABASE :"database_name" OWNER TO hub_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO hub_owner;
