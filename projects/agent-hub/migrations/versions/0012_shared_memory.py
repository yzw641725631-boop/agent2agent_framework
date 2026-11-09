"""Immutable document versions, incremental extractive memory and inherited visibility."""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    # vector is installed by the database operator, never by the runtime or worker.
    op.execute("""
      DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') THEN
          RAISE EXCEPTION 'Provision the pinned vector extension before migration 0012';
        END IF;
      END $$;
      CREATE TABLE document_versions (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), source_id uuid NOT NULL,
        configuration_version integer NOT NULL, snapshot_id uuid NOT NULL,
        repository_url varchar(400) NOT NULL, branch varchar(200) NOT NULL, git_commit varchar(64),
        path varchar(800) NOT NULL, title varchar(300) NOT NULL, content text NOT NULL,
        sha256 varchar(64) NOT NULL, content_hash varchar(64) NOT NULL,
        original_bytes bytea, exact_bytes boolean NOT NULL DEFAULT false,
        UNIQUE(source_id,configuration_version,path,sha256),
        FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id)
      );
      ALTER TABLE project_documents ADD COLUMN version_id uuid REFERENCES document_versions(id);
      CREATE TABLE memory_jobs (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id),
        worklog_id uuid UNIQUE REFERENCES worklogs(id), document_id uuid UNIQUE REFERENCES document_versions(id),
        status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','succeeded','failed')),
        attempts integer NOT NULL DEFAULT 0, finished_at timestamptz, error_code varchar(64),
        CHECK(num_nonnulls(worklog_id,document_id)=1)
      );
      CREATE INDEX ix_memory_jobs_queue ON memory_jobs(created_at,id) WHERE status='queued';
      CREATE TABLE memory_cards (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id),
        worklog_id uuid REFERENCES worklogs(id), document_id uuid REFERENCES document_versions(id),
        ordinal integer NOT NULL CHECK(ordinal>=0), title varchar(300) NOT NULL,
        body text NOT NULL CHECK(octet_length(body)<=12000), source_hash varchar(64) NOT NULL,
        line_start integer, line_end integer, truncated boolean NOT NULL DEFAULT false,
        extractor varchar(40) NOT NULL,
        CHECK(num_nonnulls(worklog_id,document_id)=1),
        UNIQUE(worklog_id,ordinal), UNIQUE(document_id,ordinal)
      );
      CREATE INDEX ix_memory_cards_project ON memory_cards(project_id,created_at DESC,id);
      CREATE TABLE memory_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        card_id uuid NOT NULL REFERENCES memory_cards(id), project_id uuid NOT NULL REFERENCES projects(id),
        version integer NOT NULL CHECK(version>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id),
        state varchar(16) NOT NULL CHECK(state IN ('active','conflicted','retired')),
        reason text NOT NULL, UNIQUE(card_id,version)
      );
      -- An empty, rebuildable semantic index. No fabricated or hashed embeddings.
      CREATE TABLE memory_embeddings (
        card_id uuid NOT NULL REFERENCES memory_cards(id), model varchar(200) NOT NULL,
        dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        content_hash varchar(64) NOT NULL, embedding vector NOT NULL,
        CHECK(vector_dims(embedding)=dimensions), PRIMARY KEY(card_id,model)
      );
      CREATE FUNCTION hub.queue_memory_source() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF TG_TABLE_NAME='worklogs' THEN
          INSERT INTO public.memory_jobs(id,project_id,worklog_id)
            VALUES(gen_random_uuid(),NEW.project_id,NEW.id) ON CONFLICT(worklog_id) DO NOTHING;
        ELSE
          INSERT INTO public.memory_jobs(id,project_id,document_id)
            VALUES(gen_random_uuid(),NEW.project_id,NEW.id) ON CONFLICT(document_id) DO NOTHING;
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_memory_source() FROM PUBLIC;
      CREATE TRIGGER memory_log_enqueued AFTER INSERT ON worklogs
        FOR EACH ROW EXECUTE FUNCTION hub.queue_memory_source();
      CREATE TRIGGER memory_document_enqueued AFTER INSERT ON document_versions
        FOR EACH ROW EXECUTE FUNCTION hub.queue_memory_source();
      GRANT USAGE ON SCHEMA public,hub TO hub_memory;
      GRANT SELECT ON document_versions,memory_jobs,memory_cards,memory_events,memory_embeddings TO hub_runtime;
      GRANT INSERT ON memory_events TO hub_runtime;
      GRANT UPDATE(status,attempts,error_code,finished_at) ON memory_jobs TO hub_runtime;
      GRANT SELECT,INSERT ON document_versions TO hub_indexer;
      GRANT SELECT,UPDATE ON memory_jobs TO hub_memory;
      GRANT SELECT,INSERT ON memory_cards TO hub_memory;
      GRANT SELECT ON document_versions,worklogs TO hub_memory;
      GRANT UPDATE(version_id) ON project_documents TO hub_indexer;
      CREATE POLICY memory_worker_logs ON worklogs FOR SELECT TO hub_memory USING(true);
    """)
    for table in ("document_versions", "memory_jobs", "memory_cards", "memory_events", "memory_embeddings"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY memory_owner ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY version_indexer ON document_versions TO hub_indexer USING(true) WITH CHECK(true);
      CREATE POLICY version_worker ON document_versions FOR SELECT TO hub_memory USING(true);
      CREATE POLICY job_worker ON memory_jobs TO hub_memory USING(true) WITH CHECK(true);
      CREATE POLICY card_worker ON memory_cards TO hub_memory USING(true) WITH CHECK(true);
      CREATE POLICY version_read ON document_versions FOR SELECT TO hub_runtime USING(
        (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id))
        AND hub.can_read_source(project_id,source_id)
        AND EXISTS(SELECT 1 FROM document_repositories r WHERE r.id=source_id
          AND r.project_id=document_versions.project_id AND r.version=configuration_version));
      -- Invoker functions deliberately see the caller's original-source RLS.
      CREATE FUNCTION hub.memory_source_visible(wid uuid,did uuid) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT CASE WHEN wid IS NOT NULL THEN EXISTS(SELECT 1 FROM public.worklogs w WHERE w.id=wid)
          ELSE EXISTS(SELECT 1 FROM public.document_versions d WHERE d.id=did) END
      $$;
      REVOKE ALL ON FUNCTION hub.memory_source_visible(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.memory_source_visible(uuid,uuid) TO hub_runtime;
      CREATE POLICY job_read ON memory_jobs FOR SELECT TO hub_runtime
        USING(hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY job_retry ON memory_jobs FOR UPDATE TO hub_runtime
        USING(status='failed' AND hub.has_project_cap(project_id,'tasks:publish')
          AND hub.memory_source_visible(worklog_id,document_id))
        WITH CHECK(status='queued' AND attempts=0 AND hub.has_project_cap(project_id,'tasks:publish')
          AND hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY card_read ON memory_cards FOR SELECT TO hub_runtime
        USING(hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY event_read ON memory_events FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=memory_events.project_id));
      CREATE POLICY event_insert ON memory_events FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=memory_events.project_id));
      CREATE POLICY embedding_read ON memory_embeddings FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id));
      -- Contexts contain derived documents as well as logs. They are personal receipts.
      DROP POLICY contexts_read ON work_contexts;
      CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING(
        hub.can_access_project(project_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'items','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'recent_unindexed','[]'::jsonb)
                            || coalesce(payload->'recent_work','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM worklogs w WHERE w.id=(item->>'id')::uuid)));
      -- Existing decoded snapshots cannot claim byte-for-byte preservation retroactively.
      INSERT INTO document_versions(id,project_id,source_id,configuration_version,snapshot_id,
        repository_url,branch,git_commit,path,title,content,sha256,content_hash)
        SELECT gen_random_uuid(),d.project_id,d.source_id,r.version,d.snapshot_id,r.repository_url,r.branch,
          r.git_commit,d.path,d.title,d.content,d.sha256,encode(sha256(convert_to(d.content,'UTF8')),'hex')
          FROM project_documents d JOIN document_repositories r ON r.id=d.source_id;
      UPDATE project_documents d SET version_id=v.id FROM document_versions v
        WHERE d.source_id=v.source_id AND d.path=v.path AND d.sha256=v.sha256;
      INSERT INTO memory_jobs(id,project_id,worklog_id)
        SELECT gen_random_uuid(),project_id,id FROM worklogs ON CONFLICT(worklog_id) DO NOTHING;
    """)


def downgrade():
    raise RuntimeError("Preserve original documents and memory provenance; roll back application images only")
