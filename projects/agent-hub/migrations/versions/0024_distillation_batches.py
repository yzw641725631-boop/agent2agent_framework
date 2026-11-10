"""Automatic bounded source batches; conclusions remain proposals by an authorized local model."""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    # The API may check compatibility, but it cannot change migration history.
    op.execute("GRANT SELECT ON alembic_version TO hub_runtime")
    op.execute("""
      CREATE TABLE distillation_policies (
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), created_at timestamptz NOT NULL DEFAULT now(),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE distillation_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        policy_version integer NOT NULL, source_refs jsonb NOT NULL,
        fingerprint varchar(64) NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        state varchar(20) NOT NULL DEFAULT 'queued'
          CHECK(state IN ('queued','running','succeeded','obsolete')),
        target_user_id uuid REFERENCES users(id), target_device_id uuid REFERENCES devices(id),
        instance_id uuid,
        lease_epoch integer NOT NULL DEFAULT 0, lease_until timestamptz, attempts integer NOT NULL DEFAULT 0,
        fact_ids jsonb NOT NULL DEFAULT '[]', outcome varchar(32), reason varchar(2000) NOT NULL DEFAULT '',
        finished_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK(jsonb_typeof(source_refs)='array' AND jsonb_array_length(source_refs) BETWEEN 1 AND 8)
      );
      CREATE TABLE distillation_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES distillation_jobs(id), job_version integer NOT NULL,
        author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        state varchar(20) NOT NULL, reason varchar(2000) NOT NULL,
        UNIQUE(job_id,job_version)
      );
      CREATE INDEX ix_distillation_queue ON distillation_jobs(repository_id,state,created_at,id);
      CREATE UNIQUE INDEX ix_distillation_batch_once
        ON distillation_jobs(repository_id,policy_version,fingerprint) WHERE state<>'obsolete';
      GRANT SELECT,INSERT ON distillation_policies TO hub_runtime;
      GRANT SELECT,UPDATE ON distillation_jobs TO hub_runtime;
      GRANT SELECT,INSERT ON distillation_events TO hub_runtime;
    """)
    for table in ("distillation_policies", "distillation_jobs", "distillation_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY distillation_policy_read ON distillation_policies FOR SELECT TO hub_runtime
        USING(hub.has_repository_cap(project_id,repository_id,'code:read'));
      CREATE POLICY distillation_policy_insert ON distillation_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY distillation_job_read ON distillation_jobs FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read') AND NOT EXISTS(
          SELECT 1 FROM jsonb_array_elements(source_refs) s WHERE NOT EXISTS(
            SELECT 1 FROM memory_cards c WHERE c.id=(s->>'source_card_id')::uuid
              AND c.project_id=distillation_jobs.project_id)));
      CREATE POLICY distillation_job_update ON distillation_jobs FOR UPDATE TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND hub.has_project_cap(project_id,'tasks:write')
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(source_refs) s WHERE NOT EXISTS(
          SELECT 1 FROM memory_cards c WHERE c.id=(s->>'source_card_id')::uuid
            AND c.project_id=distillation_jobs.project_id))) WITH CHECK(
        target_user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND target_device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY distillation_event_read ON distillation_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=job_id));
      CREATE POLICY distillation_event_insert ON distillation_events FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=job_id
          AND j.target_user_id=author_id AND j.target_device_id=device_id AND j.version=job_version));

      CREATE FUNCTION hub.enqueue_distillation(rid uuid) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE
        config jsonb; config_version integer; pid uuid; refs jsonb='[]'; source record;
        used_bytes integer=0; size_bytes integer; jid uuid;
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('distillation:'||rid::text,0));
        SELECT r.project_id INTO pid FROM project_repositories r JOIN projects p ON p.id=r.project_id
          WHERE r.id=rid AND r.status='active' AND p.status='active';
        IF pid IS NULL THEN RETURN 0; END IF;
        SELECT content,version INTO config,config_version FROM distillation_policies
          WHERE repository_id=rid ORDER BY version DESC LIMIT 1;
        -- Invalidate pending work immediately when its policy or any source is replaced/retired.
        WITH changed AS (
          UPDATE distillation_jobs j SET state='obsolete',version=j.version+1,lease_until=NULL,
            lease_epoch=j.lease_epoch+1,finished_at=now(),
            reason='Policy or source changed; originals retained'
          WHERE j.repository_id=rid AND j.state IN ('queued','running') AND (
            j.policy_version IS DISTINCT FROM config_version
            OR NOT coalesce((config->>'enabled')::boolean,false)
            OR EXISTS(SELECT 1 FROM jsonb_array_elements(j.source_refs) s
              WHERE NOT hub.memory_card_current((s->>'source_card_id')::uuid)))
          RETURNING id,version,state,reason
        ) INSERT INTO distillation_events(job_id,job_version,state,reason)
          SELECT id,version,state,reason FROM changed;
        IF NOT coalesce((config->>'enabled')::boolean,false) THEN RETURN 0; END IF;
        FOR source IN
          SELECT c.*,w.created_at AS original_created_at,d.path AS original_path
            FROM memory_cards c LEFT JOIN worklogs w ON w.id=c.worklog_id
            LEFT JOIN tasks t ON t.id=w.task_id LEFT JOIN document_versions d ON d.id=c.document_id
          WHERE c.project_id=pid AND coalesce(t.repository_id,d.source_id)=rid
            AND hub.memory_card_current(c.id)
            AND NOT EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.repository_id=rid
              AND j.policy_version=config_version AND j.state<>'obsolete'
              AND j.source_refs @> jsonb_build_array(jsonb_build_object('source_card_id',c.id::text)))
          ORDER BY c.created_at,c.id LIMIT (config->>'max_sources')::integer
        LOOP
          size_bytes=octet_length(jsonb_build_object('source_card_id',source.id::text,'source_hash',source.source_hash,
            'title',source.title,'body',source.body,'source_kind',
              CASE WHEN source.worklog_id IS NOT NULL THEN 'worklog' ELSE 'document' END,
            'source_id',coalesce(source.worklog_id,source.document_id)::text,
            'source_path',source.original_path,'source_created_at',source.original_created_at,
            'truncated',source.truncated,'content_trust','untrusted-project-data')::text);
          EXIT WHEN jsonb_array_length(refs)>=(config->>'max_sources')::integer
            OR used_bytes+size_bytes>(config->>'input_budget_bytes')::integer-2048;
          refs=refs||jsonb_build_array(jsonb_build_object('source_card_id',source.id::text,'source_hash',source.source_hash));
          used_bytes=used_bytes+size_bytes;
        END LOOP;
        IF jsonb_array_length(refs)<(config->>'min_sources')::integer THEN RETURN 0; END IF;
        INSERT INTO distillation_jobs(project_id,repository_id,policy_version,source_refs,fingerprint)
          VALUES(pid,rid,config_version,refs,encode(sha256(convert_to(refs::text,'UTF8')),'hex'))
          ON CONFLICT DO NOTHING RETURNING id INTO jid;
        IF jid IS NULL THEN RETURN 0; END IF;
        INSERT INTO distillation_events(job_id,job_version,state,reason)
          VALUES(jid,1,'queued','Automatically batched source references; no model has executed');
        RETURN 1;
      END $$;
      REVOKE ALL ON FUNCTION hub.enqueue_distillation(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.schedule_distillation() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE rid uuid; count_jobs integer=0;
      BEGIN
        FOR rid IN SELECT repository_id FROM (
          SELECT DISTINCT ON (repository_id) repository_id,content FROM distillation_policies
            ORDER BY repository_id,version DESC) current_policy
          WHERE (content->>'enabled')::boolean ORDER BY repository_id LOOP
          count_jobs=count_jobs+hub.enqueue_distillation(rid);
        END LOOP;
        RETURN count_jobs;
      END $$;
      REVOKE ALL ON FUNCTION hub.schedule_distillation() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.schedule_distillation() TO hub_memory;
      CREATE FUNCTION hub.distillation_policy_changed() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        PERFORM hub.enqueue_distillation(NEW.repository_id); RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.distillation_policy_changed() FROM PUBLIC;
      CREATE TRIGGER distillation_policy_queue AFTER INSERT ON distillation_policies
        FOR EACH ROW EXECUTE FUNCTION hub.distillation_policy_changed();

      CREATE FUNCTION hub.guard_distillation_transition() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF NEW.id<>OLD.id OR NEW.project_id<>OLD.project_id OR NEW.repository_id<>OLD.repository_id
          OR NEW.source_refs<>OLD.source_refs OR NEW.fingerprint<>OLD.fingerprint
          OR NEW.policy_version<>OLD.policy_version OR NEW.created_at<>OLD.created_at
          OR NEW.version<>OLD.version+1 OR OLD.state IN ('succeeded','obsolete')
          OR (NEW.state='running' AND NOT(OLD.state='queued' OR OLD.lease_until<=now()))
          OR (NEW.state IN ('queued','succeeded') AND OLD.state<>'running') THEN
          RAISE EXCEPTION 'Invalid distillation transition' USING ERRCODE='23514';
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER distillation_transition BEFORE UPDATE ON distillation_jobs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_distillation_transition();
      CREATE FUNCTION hub.check_distillation_event() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM distillation_events e WHERE e.job_id=NEW.id
          AND e.job_version=NEW.version AND e.state=NEW.state) THEN
          RAISE EXCEPTION 'Distillation event required' USING ERRCODE='23514';
        END IF; RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER distillation_event_evidence AFTER INSERT OR UPDATE ON distillation_jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_distillation_event();
    """)


def downgrade():
    raise RuntimeError("Preserve source batches and proposed knowledge; restore a verified backup")
