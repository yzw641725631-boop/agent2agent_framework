"""Reviewed durable facts inherit their original source and retain every decision."""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE memory_cards ADD CONSTRAINT memory_cards_identity_project UNIQUE(id,project_id);
      CREATE TABLE memory_facts (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), source_card_id uuid NOT NULL,
        resource_id uuid NOT NULL, author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id),
        topic varchar(120) NOT NULL CHECK(topic ~ '^[a-z0-9][a-z0-9._/-]{0,119}$'),
        kind varchar(20) NOT NULL CHECK(kind IN ('constraint','decision','interface','lesson','pitfall')),
        title varchar(200) NOT NULL, body text NOT NULL CHECK(octet_length(body) BETWEEN 10 AND 6000),
        source_quote text NOT NULL CHECK(octet_length(source_quote) BETWEEN 8 AND 4000),
        source_hash varchar(64) NOT NULL, content_hash varchar(64) NOT NULL, generation jsonb NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','active','conflicted','retired','rejected','superseded','source_stale')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), UNIQUE(id,project_id),
        FOREIGN KEY(source_card_id,project_id) REFERENCES memory_cards(id,project_id)
      );
      CREATE INDEX ix_memory_facts_project ON memory_facts(project_id,created_at DESC,id);
      CREATE INDEX ix_memory_facts_source ON memory_facts(source_card_id);
      CREATE UNIQUE INDEX one_active_memory_fact_topic ON memory_facts(project_id,resource_id,topic)
        WHERE status='active';
      CREATE TABLE memory_fact_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        fact_id uuid NOT NULL, project_id uuid NOT NULL REFERENCES projects(id),
        version integer NOT NULL CHECK(version>0),
        state varchar(20) NOT NULL
          CHECK(state IN ('pending','active','conflicted','retired','rejected','superseded','source_stale')),
        reason text NOT NULL, author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        override_reason text NOT NULL DEFAULT '', automatic boolean NOT NULL DEFAULT false,
        UNIQUE(fact_id,version), FOREIGN KEY(fact_id,project_id) REFERENCES memory_facts(id,project_id),
        CHECK(automatic OR (author_id IS NOT NULL AND device_id IS NOT NULL))
      );
      CREATE FUNCTION hub.memory_card_resource(cid uuid,pid uuid) RETURNS uuid
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT coalesce(t.repository_id,d.source_id,c.project_id)
          FROM public.memory_cards c LEFT JOIN public.worklogs w ON w.id=c.worklog_id
          LEFT JOIN public.tasks t ON t.id=w.task_id LEFT JOIN public.document_versions d ON d.id=c.document_id
          WHERE c.id=cid AND c.project_id=pid
      $$;
      CREATE FUNCTION hub.memory_card_current(cid uuid) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=cid
          AND coalesce((SELECT e.state FROM public.memory_events e WHERE e.card_id=c.id
                        ORDER BY e.version DESC LIMIT 1),'active')='active'
          AND (c.worklog_id IS NOT NULL OR EXISTS(
            SELECT 1 FROM public.project_documents d JOIN public.document_versions v ON v.id=d.version_id
              JOIN public.document_repositories r ON r.id=v.source_id AND r.version=v.configuration_version
              WHERE d.version_id=c.document_id)))
      $$;
      REVOKE ALL ON FUNCTION hub.memory_card_resource(uuid,uuid),hub.memory_card_current(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.memory_card_resource(uuid,uuid),hub.memory_card_current(uuid) TO hub_runtime;
      -- Definer invalidation checks the committed source as the migration owner.
      -- FORCE RLS otherwise hides current documents even from their table owner.
      -- Invoker helpers keep the runtime caller's original visibility unchanged.
      CREATE POLICY knowledge_owner_read ON project_documents FOR SELECT TO hub_owner USING(true);
      CREATE POLICY knowledge_owner_read ON document_repositories FOR SELECT TO hub_owner USING(true);
      GRANT SELECT,INSERT ON memory_facts,memory_fact_events TO hub_runtime;
      GRANT UPDATE(status,version) ON memory_facts TO hub_runtime;
    """)
    for table in ("memory_facts", "memory_fact_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY knowledge_owner ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY fact_read ON memory_facts FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id));
      CREATE POLICY fact_propose ON memory_facts FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write') AND status='pending' AND version=1
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND resource_id=hub.memory_card_resource(source_card_id,project_id)
        AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id
                   AND c.source_hash=memory_facts.source_hash AND strpos(c.body,memory_facts.source_quote)>0));
      CREATE POLICY fact_review ON memory_facts FOR UPDATE TO hub_runtime
        USING(hub.has_project_cap(project_id,'tasks:publish')
          AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id))
        WITH CHECK(hub.has_project_cap(project_id,'tasks:publish')
          AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id));
      CREATE POLICY fact_event_read ON memory_fact_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=memory_fact_events.project_id));
      CREATE POLICY fact_event_write ON memory_fact_events FOR INSERT TO hub_runtime WITH CHECK(
        NOT automatic AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=memory_fact_events.project_id
          AND ((memory_fact_events.state='pending' AND memory_fact_events.version=1
                AND f.author_id=memory_fact_events.author_id AND hub.has_project_cap(f.project_id,'tasks:write'))
               OR (memory_fact_events.state<>'pending' AND hub.has_project_cap(f.project_id,'tasks:publish')))));
      CREATE FUNCTION hub.check_memory_fact_event() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE fid uuid;
      BEGIN
        fid := CASE WHEN TG_TABLE_NAME='memory_facts' THEN (to_jsonb(NEW)->>'id')::uuid
                    ELSE (to_jsonb(NEW)->>'fact_id')::uuid END;
        IF EXISTS(SELECT 1 FROM public.memory_facts f WHERE f.id=fid AND (NOT EXISTS(
          SELECT 1 FROM public.memory_fact_events e WHERE e.fact_id=f.id AND e.version=f.version AND e.state=f.status)
          OR EXISTS(SELECT 1 FROM public.memory_fact_events e WHERE e.fact_id=f.id AND e.version>f.version)))
        THEN RAISE EXCEPTION 'Knowledge state has no matching decision'; END IF;
        RETURN NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.check_memory_fact_event() FROM PUBLIC;
      CREATE CONSTRAINT TRIGGER fact_state_has_event AFTER INSERT OR UPDATE ON memory_facts
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_memory_fact_event();
      CREATE CONSTRAINT TRIGGER fact_event_has_state AFTER INSERT ON memory_fact_events
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_memory_fact_event();
      CREATE FUNCTION hub.invalidate_memory_facts() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE changed record; new_row jsonb; old_row jsonb;
      BEGIN
        new_row := CASE WHEN TG_OP<>'DELETE' THEN to_jsonb(NEW) ELSE '{}'::jsonb END;
        old_row := CASE WHEN TG_OP<>'INSERT' THEN to_jsonb(OLD) ELSE '{}'::jsonb END;
        FOR changed IN UPDATE public.memory_facts f SET status='source_stale',version=version+1
          WHERE f.status='active' AND EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=f.source_card_id
            AND NOT hub.memory_card_current(c.id) AND (
            (TG_TABLE_NAME='memory_events' AND c.id=(new_row->>'card_id')::uuid)
            OR (TG_TABLE_NAME='project_documents' AND c.document_id=(old_row->>'version_id')::uuid)
            OR (TG_TABLE_NAME='document_repositories' AND EXISTS(SELECT 1 FROM public.document_versions d
              WHERE d.id=c.document_id AND d.source_id=(new_row->>'id')::uuid))))
          RETURNING f.id,f.project_id,f.version
        LOOP
          INSERT INTO public.memory_fact_events(id,fact_id,project_id,version,state,reason,automatic)
            VALUES(gen_random_uuid(),changed.id,changed.project_id,changed.version,'source_stale',
                   'Original source changed, retired or conflicted; review a current citation before reactivation.',true);
        END LOOP;
        RETURN NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.invalidate_memory_facts() FROM PUBLIC;
      CREATE TRIGGER knowledge_card_invalidated AFTER INSERT ON memory_events
        FOR EACH ROW WHEN(NEW.state IN ('retired','conflicted')) EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE CONSTRAINT TRIGGER knowledge_document_replaced AFTER UPDATE ON project_documents
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(OLD.version_id IS DISTINCT FROM NEW.version_id)
        EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE CONSTRAINT TRIGGER knowledge_document_removed AFTER DELETE ON project_documents
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE TRIGGER knowledge_configuration_changed AFTER UPDATE ON document_repositories
        FOR EACH ROW WHEN(OLD.version IS DISTINCT FROM NEW.version) EXECUTE FUNCTION hub.invalidate_memory_facts();
      DROP POLICY contexts_read ON work_contexts;
      CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING(
        hub.can_access_project(project_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'items','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'knowledge','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'recent_unindexed','[]'::jsonb)
                            || coalesce(payload->'recent_work','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM worklogs w WHERE w.id=(item->>'id')::uuid)));
    """)


def downgrade():
    raise RuntimeError("Retain knowledge sources and review history; roll back application images only")
