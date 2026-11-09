"""Source-frozen per-contributor guide jobs and independently reviewed publication."""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE tasks ADD COLUMN documentation jsonb NOT NULL DEFAULT '{"kind":"none"}';
      -- The trigger may inspect all frozen contributor records. Runtime log policies stay unchanged.
      CREATE POLICY guide_source_owner ON worklogs FOR SELECT TO hub_owner USING(true);
      CREATE TABLE guide_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        task_id uuid NOT NULL REFERENCES tasks(id), source_worklog_id uuid NOT NULL UNIQUE REFERENCES worklogs(id),
        source_hash varchar(64) NOT NULL, target_user_id uuid NOT NULL REFERENCES users(id),
        target_device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        harness varchar(24) NOT NULL, plan jsonb NOT NULL,
        state varchar(20) NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','review','approved','published')),
        version integer NOT NULL DEFAULT 1, lease_epoch integer NOT NULL DEFAULT 0, lease_until timestamptz,
        draft_version integer NOT NULL DEFAULT 0, audience varchar(16) NOT NULL DEFAULT 'repository'
          CHECK(audience IN ('repository','project')),
        reason varchar(2000) NOT NULL DEFAULT '', published_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_guide_jobs_inbox ON guide_jobs(project_id,target_device_id,created_at,id);
      CREATE TABLE guide_requirements (
        submission_id uuid NOT NULL REFERENCES worklogs(id), job_id uuid NOT NULL REFERENCES guide_jobs(id),
        PRIMARY KEY(submission_id,job_id)
      );
      CREATE TABLE guide_revisions (
        job_id uuid NOT NULL REFERENCES guide_jobs(id), version integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        PRIMARY KEY(job_id,version)
      );
      CREATE TABLE guide_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), job_id uuid NOT NULL REFERENCES guide_jobs(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        kind varchar(24) NOT NULL, draft_version integer NOT NULL, payload jsonb NOT NULL
      );
      CREATE FUNCTION hub.queue_submission_guides() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NEW.submission_id IS NULL OR NEW.submission_id IS NOT DISTINCT FROM OLD.submission_id
          OR NEW.documentation->>'kind'='none' THEN RETURN NEW; END IF;
        WITH contributors AS (
          SELECT DISTINCT ON (r.user_id,r.device_id,r.instance_id) w.*, r.instance_id, r.harness
          FROM public.worklogs w JOIN public.task_runs r ON r.id=w.run_id
          WHERE w.task_id=NEW.id AND w.closeout_id IS NULL AND w.outcome='checkpoint'
          ORDER BY r.user_id,r.device_id,r.instance_id,w.created_at DESC,w.id
        ) INSERT INTO public.guide_jobs(project_id,repository_id,task_id,source_worklog_id,source_hash,
          target_user_id,target_device_id,instance_id,harness,plan)
          SELECT NEW.project_id,NEW.repository_id,NEW.id,c.id,c.receipt_hash,c.author_id,c.device_id,
            c.instance_id,c.harness,NEW.documentation FROM contributors c ON CONFLICT(source_worklog_id) DO NOTHING;
        INSERT INTO public.guide_requirements(submission_id,job_id)
          SELECT NEW.submission_id,j.id FROM public.guide_jobs j JOIN (
            SELECT DISTINCT ON (r.user_id,r.device_id,r.instance_id) w.id
            FROM public.worklogs w JOIN public.task_runs r ON r.id=w.run_id
            WHERE w.task_id=NEW.id AND w.closeout_id IS NULL AND w.outcome='checkpoint'
            ORDER BY r.user_id,r.device_id,r.instance_id,w.created_at DESC,w.id
          ) c ON c.id=j.source_worklog_id ON CONFLICT DO NOTHING;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_submission_guides() FROM PUBLIC;
      CREATE TRIGGER queue_submission_guides AFTER UPDATE OF submission_id ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.queue_submission_guides();
      CREATE FUNCTION hub.guide_release_ready(tid uuid,wid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.submission_id=wid
          AND hub.can_read_scope(t.project_id,t.repository_id))
          AND EXISTS(SELECT 1 FROM public.guide_requirements WHERE submission_id=wid)
          AND NOT EXISTS(SELECT 1 FROM public.guide_requirements q JOIN public.guide_jobs j ON j.id=q.job_id
            WHERE q.submission_id=wid AND j.state NOT IN ('approved','published'))
      $$;
      REVOKE ALL ON FUNCTION hub.guide_release_ready(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guide_release_ready(uuid,uuid) TO hub_runtime;
      GRANT SELECT ON guide_jobs,guide_requirements,guide_revisions,guide_events TO hub_runtime;
      GRANT INSERT ON guide_revisions,guide_events TO hub_runtime;
      GRANT UPDATE(target_user_id,target_device_id,instance_id,harness,state,version,lease_epoch,lease_until,
        draft_version,audience,reason,published_at) ON guide_jobs TO hub_runtime;
    """)
    for table in ("guide_jobs", "guide_requirements", "guide_revisions", "guide_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY job_read ON guide_jobs FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) OR
        state='published' AND audience='project' AND hub.has_project_cap(project_id,'guides:read'));
      CREATE POLICY job_update ON guide_jobs FOR UPDATE TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          hub.has_project_cap(project_id,'tasks:publish') OR hub.has_project_cap(project_id,'tasks:test')
          OR target_user_id=nullif(current_setting('hub.user_id',true),'')::uuid
            AND target_device_id=nullif(current_setting('hub.device_id',true),'')::uuid
            AND hub.has_project_cap(project_id,'tasks:write')))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY requirement_read ON guide_requirements FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id));
      CREATE POLICY revision_read ON guide_revisions FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND
          (hub.can_read_scope(j.project_id,j.repository_id) OR guide_revisions.version=j.draft_version)));
      CREATE POLICY revision_insert ON guide_revisions FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND j.target_user_id=author_id
          AND j.target_device_id=device_id AND j.state='running'
          AND hub.can_read_scope(j.project_id,j.repository_id) AND hub.has_project_cap(j.project_id,'tasks:write')));
      CREATE POLICY event_read ON guide_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND hub.can_read_scope(j.project_id,j.repository_id)));
      CREATE POLICY event_insert ON guide_events FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND hub.can_read_scope(j.project_id,j.repository_id)
          AND (hub.has_project_cap(j.project_id,'tasks:write') OR hub.has_project_cap(j.project_id,'tasks:publish')
            OR hub.has_project_cap(j.project_id,'tasks:test'))));
    """)
    op.execute("""
      ALTER TABLE guide_events ADD CONSTRAINT guide_event_version_required
        CHECK(payload ? 'job_version' AND jsonb_typeof(payload->'job_version')='number'
          AND (payload->>'job_version')::integer>1);
      ALTER TABLE guide_events ADD CONSTRAINT guide_review_fields_required CHECK(kind<>'reviewed' OR
        payload ?& ARRAY['expected_version','draft_version','gate','approved','reason','audience','override_reason']
          AND jsonb_typeof(payload->'approved')='boolean' AND payload->>'gate' IN ('product','test')
          AND length(trim(payload->>'reason'))>=10);
      CREATE UNIQUE INDEX uq_guide_event_version ON guide_events(job_id,((payload->>'job_version')::integer));
      CREATE FUNCTION hub.guard_guide_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid;
        writer boolean; reviewer boolean; publisher boolean; reassigned boolean;
      BEGIN
        IF current_user='hub_owner' THEN RETURN NEW; END IF;
        writer:=hub.has_project_cap(NEW.project_id,'tasks:write') AND OLD.target_user_id=uid
          AND OLD.target_device_id=did AND (NEW.repository_id IS NULL OR
            hub.has_repository_cap(NEW.project_id,NEW.repository_id,'code:write'));
        publisher:=hub.has_project_cap(NEW.project_id,'tasks:publish');
        reviewer:=publisher OR hub.has_project_cap(NEW.project_id,'tasks:test');
        reassigned:=NEW.lease_epoch=OLD.lease_epoch+1 AND NEW.state='queued' AND publisher;
        IF NEW.version<>OLD.version+1 OR OLD.state='published'
          OR NEW.draft_version<OLD.draft_version OR NEW.draft_version>OLD.draft_version+1
          OR (NEW.target_user_id,NEW.target_device_id,NEW.instance_id,NEW.harness)
              IS DISTINCT FROM (OLD.target_user_id,OLD.target_device_id,OLD.instance_id,OLD.harness) AND NOT reassigned
          OR NEW.audience<>OLD.audience AND NOT (OLD.state='review' AND NEW.state IN ('review','approved') AND publisher)
          OR (NEW.state='published')<>(NEW.published_at IS NOT NULL)
          OR (NEW.state='running')<>(NEW.lease_until IS NOT NULL)
        THEN RAISE EXCEPTION 'invalid guide transition' USING ERRCODE='23514'; END IF;
        IF reassigned THEN
          IF length(trim(NEW.reason))<10 OR NEW.draft_version<>OLD.draft_version OR NOT EXISTS(
            SELECT 1 FROM public.agent_endpoints a WHERE a.project_id=NEW.project_id
              AND a.repository_id IS NOT DISTINCT FROM NEW.repository_id AND a.user_id=NEW.target_user_id
              AND a.device_id=NEW.target_device_id AND a.instance_id=NEW.instance_id AND a.harness=NEW.harness
              AND hub.agent_endpoint_eligible(a.id))
          THEN RAISE EXCEPTION 'guide reassignment requires eligible recipient' USING ERRCODE='23514'; END IF;
        ELSIF NEW.state='running' THEN
          IF NOT writer OR NOT (OLD.state='queued' OR OLD.state='running' AND OLD.lease_until<=now())
            OR NEW.lease_epoch<>OLD.lease_epoch+1 OR NEW.lease_until<=now()
            OR NEW.lease_until>now()+interval '11 minutes' OR NEW.draft_version<>OLD.draft_version
          THEN RAISE EXCEPTION 'invalid guide claim' USING ERRCODE='23514'; END IF;
        ELSIF OLD.state='running' THEN
          IF NOT writer OR OLD.lease_until<=now() OR NEW.lease_epoch<>OLD.lease_epoch
            OR NEW.state NOT IN ('queued','review')
            OR NEW.draft_version<>OLD.draft_version+(CASE WHEN NEW.state='review' THEN 1 ELSE 0 END)
            OR NEW.state='queued' AND length(trim(NEW.reason))<10
          THEN RAISE EXCEPTION 'invalid guide generation' USING ERRCODE='23514'; END IF;
        ELSIF OLD.state='review' THEN
          IF NOT reviewer OR NEW.state NOT IN ('queued','review','approved')
            OR NEW.lease_epoch<>OLD.lease_epoch OR NEW.draft_version<>OLD.draft_version
          THEN RAISE EXCEPTION 'invalid guide review' USING ERRCODE='23514'; END IF;
        ELSIF NOT (OLD.state='approved' AND NEW.state='published' AND publisher
          AND NEW.draft_version=OLD.draft_version AND NEW.lease_epoch=OLD.lease_epoch) THEN
          RAISE EXCEPTION 'invalid guide publication' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_guide_transition() FROM PUBLIC;
      CREATE TRIGGER guide_transition BEFORE UPDATE ON guide_jobs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_guide_transition();
      CREATE FUNCTION hub.check_guide_transition_evidence() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid;
        e public.guide_events; r public.guide_revisions; expected_kind text;
      BEGIN
        IF uid IS NULL THEN RETURN NEW; END IF;
        expected_kind:=CASE WHEN NEW.state='running' THEN 'claimed'
          WHEN NEW.state='published' THEN 'published'
          WHEN NEW.state='queued' AND NEW.lease_epoch>OLD.lease_epoch THEN 'reassigned'
          WHEN OLD.state='running' AND NEW.state='queued' THEN 'retry'
          WHEN OLD.state='running' AND NEW.state='review' THEN 'submitted' ELSE 'reviewed' END;
        SELECT * INTO e FROM public.guide_events WHERE job_id=NEW.id
          AND (payload->>'job_version')::integer=NEW.version;
        IF NOT FOUND OR e.author_id<>uid OR e.device_id<>did OR e.kind<>expected_kind
          OR e.draft_version<>NEW.draft_version
        THEN RAISE EXCEPTION 'guide transition requires matching immutable event' USING ERRCODE='23514'; END IF;
        SELECT * INTO r FROM public.guide_revisions WHERE job_id=NEW.id AND version=NEW.draft_version;
        IF expected_kind='submitted' AND (r.job_id IS NULL OR r.author_id<>uid OR r.device_id<>did)
        THEN RAISE EXCEPTION 'guide submission requires authored revision' USING ERRCODE='23514'; END IF;
        IF expected_kind='reviewed' THEN
          IF r.job_id IS NULL OR (e.payload->>'expected_version')::integer<>OLD.version
            OR (e.payload->>'draft_version')::integer<>NEW.draft_version
            OR e.payload->>'gate' NOT IN ('product','test')
            OR NOT hub.has_project_cap(NEW.project_id,
              CASE WHEN e.payload->>'gate'='test' THEN 'tasks:test' ELSE 'tasks:publish' END)
            OR r.author_id=uid AND NOT (EXISTS(SELECT 1 FROM public.users WHERE id=uid AND platform_role='superadmin')
              AND length(trim(e.payload->>'override_reason'))>=10)
            OR r.author_id<>uid AND length(coalesce(e.payload->>'override_reason',''))>0
            OR (SELECT count(*) FROM public.guide_events x WHERE x.job_id=NEW.id AND x.kind='reviewed'
              AND x.draft_version=NEW.draft_version AND x.payload->>'gate'=e.payload->>'gate')<>1
            OR (e.payload->>'approved')::boolean AND NEW.state NOT IN ('review','approved')
            OR NOT (e.payload->>'approved')::boolean AND NEW.state<>'queued'
          THEN RAISE EXCEPTION 'invalid independent guide review evidence' USING ERRCODE='23514'; END IF;
        END IF;
        IF NEW.state IN ('approved','published') AND (
          SELECT count(DISTINCT x.payload->>'gate') FROM public.guide_events x WHERE x.job_id=NEW.id
            AND x.kind='reviewed' AND x.draft_version=NEW.draft_version
            AND x.payload->>'gate' IN ('product','test') AND (x.payload->>'approved')::boolean)<>2
        THEN RAISE EXCEPTION 'guide requires product and test approval' USING ERRCODE='23514'; END IF;
        IF NEW.audience<>OLD.audience AND NOT (expected_kind='reviewed' AND e.payload->>'gate'='product'
          AND (e.payload->>'approved')::boolean AND e.payload->>'audience'=NEW.audience)
        THEN RAISE EXCEPTION 'guide audience requires explicit product approval' USING ERRCODE='23514'; END IF;
        IF NEW.state='published' AND NOT EXISTS(SELECT 1 FROM public.tasks t JOIN public.guide_requirements q
          ON q.submission_id=t.submission_id WHERE q.job_id=NEW.id AND t.id=NEW.task_id AND t.status='completed')
        THEN RAISE EXCEPTION 'guide publication requires released submission' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.check_guide_transition_evidence() FROM PUBLIC;
      CREATE CONSTRAINT TRIGGER guide_transition_evidence AFTER UPDATE ON guide_jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_guide_transition_evidence();
    """)


def downgrade():
    raise RuntimeError("Preserve guide drafts and review history; restore a verified backup")
