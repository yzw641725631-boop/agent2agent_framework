"""Versioned repository review rules, durable independent audits and fail-closed gates."""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE code_audit_policies (
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), created_at timestamptz NOT NULL DEFAULT now(),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK(jsonb_typeof(content->'enabled')='boolean'),
        CHECK(jsonb_typeof(content->'rules')='array' AND jsonb_array_length(content->'rules')<=60),
        CHECK(content->'blocking_severities' @> '["critical","high"]'::jsonb),
        CHECK(length(content->>'change_reason') BETWEEN 10 AND 2000),
        CHECK(NOT (content->>'enabled')::boolean OR jsonb_array_length(content->'rules')>0)
      );
      CREATE TABLE code_audits (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        task_id uuid NOT NULL REFERENCES tasks(id), worklog_id uuid NOT NULL REFERENCES worklogs(id),
        delivery_id uuid NOT NULL REFERENCES code_deliveries(id),
        policy_version integer NOT NULL, repository_version integer NOT NULL,
        delivery_policy_version integer NOT NULL, project_policy_version integer NOT NULL,
        git_commit varchar(64) NOT NULL, source_hash varchar(64) NOT NULL UNIQUE, task_context jsonb NOT NULL,
        state varchar(16) NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), lease_epoch integer NOT NULL DEFAULT 0,
        lease_until timestamptz, reviewer_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        instance_id uuid, override_reason varchar(2000) NOT NULL DEFAULT '', reason varchar(2000) NOT
        NULL DEFAULT '',
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        FOREIGN KEY(repository_id,policy_version) REFERENCES code_audit_policies(repository_id,version),
        CHECK((state='running')=(lease_until IS NOT NULL))
      );
      CREATE INDEX ix_code_audits_inbox ON code_audits(project_id,repository_id,state,created_at DESC,id);
      CREATE INDEX ix_code_audits_task ON code_audits(task_id,worklog_id,created_at DESC,id);
      CREATE TABLE code_audit_reports (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), job_id uuid NOT NULL
        REFERENCES code_audits(id),
        lease_epoch integer NOT NULL CHECK(lease_epoch>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        source_hash varchar(64) NOT NULL, content jsonb NOT NULL, passed boolean NOT NULL,
        receipt_hash varchar(64) NOT NULL, UNIQUE(job_id,lease_epoch)
      );
      CREATE TABLE code_audit_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES code_audits(id), author_id uuid REFERENCES users(id),
        device_id uuid REFERENCES devices(id), kind varchar(24) NOT NULL, version integer NOT NULL,
        payload jsonb NOT NULL, UNIQUE(job_id,version)
      );
      GRANT SELECT,INSERT ON code_audit_policies,code_audit_reports TO hub_runtime;
      GRANT SELECT ON code_audits,code_audit_events TO hub_runtime;
      GRANT UPDATE(state,version,lease_epoch,lease_until,reviewer_id,device_id,instance_id,
        override_reason,reason)
        ON code_audits TO hub_runtime;
    """)
    for table in ("code_audit_policies", "code_audits", "code_audit_reports", "code_audit_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY code_audit_progress_owner ON project_progress FOR INSERT TO hub_owner WITH CHECK(true);
      CREATE POLICY audit_policy_read ON code_audit_policies FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read'));
      CREATE POLICY audit_policy_insert ON code_audit_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read') AND
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY code_audit_read ON code_audits FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id)
        AND EXISTS(SELECT 1 FROM worklogs w WHERE w.id=worklog_id AND w.task_id=code_audits.task_id));
      CREATE POLICY code_audit_update ON code_audits FOR UPDATE TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:review'))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY code_report_read ON code_audit_reports FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id));
      CREATE POLICY code_report_insert ON code_audit_reports FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id AND j.state='running'
          AND j.reviewer_id=author_id AND j.device_id=code_audit_reports.device_id
          AND j.instance_id=code_audit_reports.instance_id AND j.lease_epoch=code_audit_reports.lease_epoch
          AND j.lease_until>now() AND hub.has_project_cap(j.project_id,'tasks:review')
          AND hub.has_project_cap(j.project_id,'logs:read')));
      CREATE POLICY code_event_read ON code_audit_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id));

      CREATE FUNCTION hub.code_audit_task_context(tid uuid) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT jsonb_build_object('id',t.id,'title',t.title,'description',t.description,
          'acceptance',t.acceptance,'scope',t.scope,'execution_role',t.execution_role)
          FROM public.tasks t WHERE t.id=tid
      $$;
      REVOKE ALL ON FUNCTION hub.code_audit_task_context(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.enqueue_code_audit(did uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE d code_deliveries%ROWTYPE; p code_audit_policies%ROWTYPE;
        rv integer; dv integer; pv integer; ctx jsonb; fingerprint text; jid uuid;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=did;
        IF d.id IS NULL THEN RETURN NULL; END IF;
        SELECT * INTO p FROM public.code_audit_policies WHERE repository_id=d.repository_id
          ORDER BY version DESC LIMIT 1;
        IF p.repository_id IS NULL OR NOT (p.content->>'enabled')::boolean THEN RETURN NULL; END IF;
        SELECT version INTO rv FROM public.project_repositories WHERE id=d.repository_id;
        SELECT coalesce(max(version),0) INTO dv FROM public.delivery_policies WHERE
        repository_id=d.repository_id;
        SELECT coalesce(max(version),0) INTO pv FROM public.project_policies WHERE project_id=d.project_id;
        ctx=hub.code_audit_task_context(d.task_id);
        fingerprint=encode(sha256(convert_to(concat_ws(':',d.id,p.version,rv,dv,pv,ctx::text),'UTF8')),'hex');
        INSERT INTO public.code_audits(project_id,repository_id,task_id,worklog_id,delivery_id,policy_version,
          repository_version,delivery_policy_version,project_policy_version,git_commit,source_hash,task_context)
          VALUES(d.project_id,d.repository_id,d.task_id,d.worklog_id,d.id,p.version,rv,dv,pv,d.git_commit,fingerprint,ctx)
          ON CONFLICT(source_hash) DO NOTHING RETURNING id INTO jid;
        IF jid IS NULL THEN SELECT id INTO jid FROM public.code_audits WHERE source_hash=fingerprint; END IF;
        RETURN jid;
      END $$;
      REVOKE ALL ON FUNCTION hub.enqueue_code_audit(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.request_code_audit(did uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE d code_deliveries%ROWTYPE;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=did;
        IF d.id IS NULL OR NOT hub.can_read_scope(d.project_id,d.repository_id)
          OR NOT (hub.has_project_cap(d.project_id,'tasks:write') OR
        hub.has_project_cap(d.project_id,'tasks:review')
          OR hub.has_project_cap(d.project_id,'tasks:publish')) THEN
          RAISE EXCEPTION 'audit not accessible' USING ERRCODE='42501'; END IF;
        RETURN hub.enqueue_code_audit(did);
      END $$;
      REVOKE ALL ON FUNCTION hub.request_code_audit(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.request_code_audit(uuid) TO hub_runtime;

      CREATE FUNCTION hub.queue_code_audits() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE did uuid;
      BEGIN
        IF TG_TABLE_NAME='code_deliveries' THEN PERFORM hub.enqueue_code_audit(NEW.id); RETURN NEW; END IF;
        FOR did IN SELECT DISTINCT ON (d.task_id) d.id FROM public.code_deliveries d
          JOIN public.tasks t ON t.id=d.task_id
          WHERE t.status NOT IN ('completed','cancelled') AND
            CASE WHEN TG_TABLE_NAME IN ('code_audit_policies','delivery_policies') THEN
        d.repository_id=(to_jsonb(NEW)->>'repository_id')::uuid
              WHEN TG_TABLE_NAME='project_repositories' THEN d.repository_id=(to_jsonb(NEW)->>'id')::uuid
              WHEN TG_TABLE_NAME='project_policies' THEN d.project_id=(to_jsonb(NEW)->>'project_id')::uuid
              ELSE d.task_id=(to_jsonb(NEW)->>'id')::uuid END
          ORDER BY d.task_id,d.created_at DESC,d.id DESC
        LOOP PERFORM hub.enqueue_code_audit(did); END LOOP;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_code_audits() FROM PUBLIC;
      CREATE TRIGGER queue_code_delivery_audit AFTER INSERT ON code_deliveries
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_policy_audit AFTER INSERT ON code_audit_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_ci_policy_audit AFTER INSERT ON delivery_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_repository_audit AFTER UPDATE OF version ON project_repositories
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_project_policy_audit AFTER INSERT ON project_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_task_audit AFTER UPDATE OF
        title,description,acceptance,scope,execution_role ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();

      CREATE FUNCTION hub.code_audit_current(jid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.code_audits j JOIN public.tasks t ON t.id=j.task_id
          JOIN public.project_repositories r ON r.id=j.repository_id
          WHERE j.id=jid AND hub.can_read_scope(j.project_id,j.repository_id)
          AND j.repository_version=r.version AND r.status='active'
          AND j.task_context=hub.code_audit_task_context(t.id)
          AND j.policy_version=(SELECT max(version) FROM public.code_audit_policies WHERE repository_id=r.id)
          AND (SELECT (content->>'enabled')::boolean FROM public.code_audit_policies
            WHERE repository_id=r.id ORDER BY version DESC LIMIT 1)
          AND j.delivery_policy_version=(SELECT coalesce(max(version),0) FROM public.delivery_policies
        WHERE repository_id=r.id)
          AND j.project_policy_version=(SELECT coalesce(max(version),0) FROM public.project_policies
        WHERE project_id=j.project_id)
          AND j.delivery_id=(SELECT d.id FROM public.code_deliveries d WHERE d.task_id=t.id
            ORDER BY d.created_at DESC,d.id DESC LIMIT 1)
          AND (t.status NOT IN ('review','testing','release_ready','completed') OR
        t.submission_id=j.worklog_id))
      $$;
      REVOKE ALL ON FUNCTION hub.code_audit_current(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.code_audit_current(uuid) TO hub_runtime;

      CREATE FUNCTION hub.code_audit_ready(tid uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE t tasks%ROWTYPE; p code_audit_policies%ROWTYPE; j code_audits%ROWTYPE;
        config jsonb; cv integer; workflow text; received delivery_evidence%ROWTYPE;
      BEGIN
        SELECT * INTO t FROM public.tasks WHERE id=tid;
        IF t.id IS NULL OR NOT hub.can_read_scope(t.project_id,t.repository_id) THEN RETURN false; END IF;
        SELECT * INTO p FROM public.code_audit_policies WHERE repository_id=t.repository_id ORDER BY
        version DESC LIMIT 1;
        IF p.repository_id IS NULL OR NOT (p.content->>'enabled')::boolean THEN RETURN true; END IF;
        SELECT * INTO j FROM public.code_audits WHERE task_id=t.id AND worklog_id=t.submission_id
          AND hub.code_audit_current(id) ORDER BY created_at DESC,id DESC LIMIT 1;
        IF j.id IS NULL OR j.state<>'completed' OR NOT EXISTS(SELECT 1 FROM public.code_audit_reports
          WHERE job_id=j.id AND lease_epoch=j.lease_epoch AND passed) THEN RETURN false; END IF;
        SELECT content,version INTO config,cv FROM public.delivery_policies WHERE
        repository_id=t.repository_id
          ORDER BY version DESC LIMIT 1;
        IF config IS NULL OR NOT config->'required_kinds' @> '["ci"]'::jsonb
          OR jsonb_array_length(config->'workflows')=0 OR config->>'ci_target'<>'delivered' THEN RETURN
        false; END IF;
        FOR workflow IN SELECT jsonb_array_elements_text(config->'workflows') LOOP
          SELECT * INTO received FROM public.delivery_evidence e WHERE e.delivery_id=j.delivery_id AND
        e.kind='ci'
            AND e.policy_version=cv AND e.repository_version=j.repository_version AND
        e.observation->>'workflow'=workflow
            ORDER BY e.observation->>'observed_at' DESC,e.created_at DESC,e.id LIMIT 1;
          IF received.id IS NULL OR NOT received.successful
            OR received.observation->>'verified_commit' IS DISTINCT FROM j.git_commit
            OR (received.observation->>'observed_at')::timestamptz<now()-
              make_interval(secs=>(config->>'max_age_seconds')::integer)
          THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
      END $$;
      REVOKE ALL ON FUNCTION hub.code_audit_ready(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.code_audit_ready(uuid) TO hub_runtime;

      CREATE FUNCTION hub.guard_code_audit_update() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid; claiming boolean; own boolean;
      BEGIN
        IF NOT hub.can_read_scope(NEW.project_id,NEW.repository_id)
          OR NOT hub.has_project_cap(NEW.project_id,'tasks:review') OR NOT
        hub.has_project_cap(NEW.project_id,'logs:read')
          OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'invalid audit transition' USING
        ERRCODE='23514'; END IF;
        claiming=NEW.state='running' AND NEW.lease_epoch=OLD.lease_epoch+1
          AND (OLD.state='queued' OR OLD.state='running' AND OLD.lease_until<=now());
        own=(OLD.reviewer_id,OLD.device_id) IS NOT DISTINCT FROM (uid,did);
        IF claiming THEN
          IF NOT hub.code_audit_current(NEW.id) OR NEW.reviewer_id<>uid OR NEW.device_id<>did OR
        NEW.instance_id IS NULL
            OR NEW.lease_until<=now() OR NEW.lease_until>now()+interval '16 minutes' THEN
            RAISE EXCEPTION 'invalid audit claim' USING ERRCODE='23514'; END IF;
          IF EXISTS(SELECT 1 FROM public.worklogs WHERE task_id=NEW.task_id AND author_id=uid) THEN
            IF NOT hub.is_superadmin() OR length(trim(NEW.override_reason))<10 THEN
              RAISE EXCEPTION 'independent audit required' USING ERRCODE='42501'; END IF;
          ELSIF NEW.override_reason<>'' THEN RAISE EXCEPTION 'invalid self review override' USING
        ERRCODE='23514'; END IF;
        ELSE
          IF (NEW.reviewer_id,NEW.device_id,NEW.instance_id,NEW.lease_epoch,NEW.override_reason)
              IS DISTINCT FROM
        (OLD.reviewer_id,OLD.device_id,OLD.instance_id,OLD.lease_epoch,OLD.override_reason)
          THEN RAISE EXCEPTION 'audit lease immutable' USING ERRCODE='23514'; END IF;
          IF NEW.state='queued' THEN
            IF length(trim(NEW.reason))<10 OR OLD.state='running' AND OLD.lease_until>now() AND NOT own
              OR OLD.state='completed' AND NOT hub.has_project_cap(NEW.project_id,'tasks:publish') THEN
              RAISE EXCEPTION 'invalid audit retry' USING ERRCODE='23514'; END IF;
          ELSIF NOT own OR OLD.state<>'running' OR OLD.lease_until<=now() OR NOT
        hub.code_audit_current(NEW.id) THEN
            RAISE EXCEPTION 'audit lease expired' USING ERRCODE='23514';
          ELSIF NEW.state='completed' THEN
            IF NOT EXISTS(SELECT 1 FROM public.code_audit_reports WHERE job_id=NEW.id AND
        lease_epoch=NEW.lease_epoch) THEN
              RAISE EXCEPTION 'audit report required' USING ERRCODE='23514'; END IF;
          ELSIF NEW.state<>'running' OR NEW.lease_until<=now() OR NEW.lease_until>now()+interval
        '16 minutes' THEN
            RAISE EXCEPTION 'invalid audit heartbeat' USING ERRCODE='23514';
          END IF;
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_update() FROM PUBLIC;
      CREATE TRIGGER code_audit_update_guard BEFORE UPDATE ON code_audits
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_update();

      CREATE FUNCTION hub.guard_code_audit_report() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE j code_audits%ROWTYPE; config jsonb; paths jsonb; checks jsonb; rules jsonb; ok boolean;
      BEGIN
        SELECT * INTO j FROM public.code_audits WHERE id=NEW.job_id FOR UPDATE;
        IF j.state<>'running' OR j.lease_until<=now() OR NOT hub.code_audit_current(j.id)
          OR (j.reviewer_id,j.device_id,j.instance_id,j.lease_epoch,j.source_hash) IS DISTINCT FROM
            (NEW.author_id,NEW.device_id,NEW.instance_id,NEW.lease_epoch,NEW.source_hash)
          OR NEW.author_id IS DISTINCT FROM nullif(current_setting('hub.user_id',true),'')::uuid
          OR NEW.device_id IS DISTINCT FROM nullif(current_setting('hub.device_id',true),'')::uuid
          OR NOT hub.has_project_cap(j.project_id,'tasks:review') OR NOT
        hub.has_project_cap(j.project_id,'logs:read')
        THEN RAISE EXCEPTION 'invalid audit report lease' USING ERRCODE='42501'; END IF;
        SELECT content INTO config FROM public.code_audit_policies WHERE repository_id=j.repository_id
        AND version=j.policy_version;
        SELECT changed_paths INTO paths FROM public.worklogs WHERE id=j.worklog_id;
        SELECT coalesce(jsonb_object_agg(v->>'id',true),'{}') INTO rules FROM
        jsonb_array_elements(config->'rules') v;
        rules=rules || jsonb_build_object('scope',true,'security',true,'correctness',true);
        checks=NEW.content->'rule_checks';
        IF octet_length(NEW.content::text)>131072 OR NEW.content->>'checkout_commit' IS DISTINCT FROM
        j.git_commit
          OR jsonb_typeof(checks) IS DISTINCT FROM 'object' OR
        jsonb_typeof(NEW.content->'reviewed_paths') IS DISTINCT FROM 'array'
          OR jsonb_typeof(NEW.content->'gaps') IS DISTINCT FROM 'array'
          OR jsonb_typeof(NEW.content->'findings') IS DISTINCT FROM 'array'
          OR length(trim(NEW.content->>'summary')) NOT BETWEEN 10 AND 4000
          OR length(trim(NEW.content->>'verification')) NOT BETWEEN 10 AND 4000
          OR NEW.content->>'conclusion' NOT IN ('pass','fail','incomplete')
          OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(checks) key) IS DISTINCT FROM
             (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(rules) key)
          OR EXISTS(SELECT 1 FROM jsonb_each_text(checks) WHERE value NOT IN
        ('pass','fail','unverified','not_applicable'))
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.content->'findings') f
            WHERE f->>'severity' IS NULL OR f->>'severity' NOT IN ('critical','high','medium','low','info')
              OR f->>'rule_id' IS NULL OR NOT rules ? (f->>'rule_id')
              OR jsonb_typeof(f->'path') IS DISTINCT FROM 'string'
              OR jsonb_typeof(f->'line') IS DISTINCT FROM 'number'
              OR length(trim(f->>'evidence')) NOT BETWEEN 10 AND 2000
              OR length(trim(f->>'recommendation')) NOT BETWEEN 10 AND 2000)
        THEN RAISE EXCEPTION 'invalid audit report content' USING ERRCODE='23514'; END IF;
        ok=NEW.content->>'conclusion'='pass' AND NEW.content->>'source_basis'='exact_checkout'
          AND jsonb_array_length(NEW.content->'gaps')=0 AND NEW.content->'reviewed_paths' @> paths
          AND NOT EXISTS(SELECT 1 FROM jsonb_each_text(checks) WHERE value IN ('fail','unverified'))
          AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.content->'findings') f
            WHERE config->'blocking_severities' ? (f->>'severity'));
        IF NEW.passed IS DISTINCT FROM ok OR NEW.content->>'conclusion'='pass' AND NOT ok THEN
          RAISE EXCEPTION 'incomplete audit cannot pass' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_report() FROM PUBLIC;
      CREATE TRIGGER code_audit_report_guard BEFORE INSERT ON code_audit_reports
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_report();

      CREATE FUNCTION hub.record_code_audit_event() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid; did uuid; d code_deliveries%ROWTYPE; label text;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=NEW.delivery_id;
        uid=coalesce(nullif(current_setting('hub.user_id',true),'')::uuid,d.author_id);
        did=coalesce(nullif(current_setting('hub.device_id',true),'')::uuid,d.device_id);
        label=CASE WHEN TG_OP='INSERT' THEN 'queued' WHEN NEW.state='queued' THEN 'retry'
          WHEN NEW.state='completed' THEN 'completed' WHEN NEW.lease_epoch>OLD.lease_epoch THEN 'claimed'
        ELSE 'heartbeat' END;
        INSERT INTO public.code_audit_events(job_id,author_id,device_id,kind,version,payload)
          VALUES(NEW.id,uid,did,label,NEW.version,jsonb_build_object('state',NEW.state,'lease_epoch',NEW.lease_epoch,
          'reason',NEW.reason,'self_review',NEW.override_reason<>'','override_reason',NEW.override_reason));
        IF label<>'heartbeat' THEN
          INSERT INTO
        public.project_progress(project_id,repository_id,author_id,device_id,status,summary,detail,
            task_reference,git_commit,kind,task_id,worklog_id)
          VALUES(NEW.project_id,NEW.repository_id,uid,did,'checkpoint','代码规范审计 · '||label,NEW.reason,
            NEW.task_id::text,NEW.git_commit,'code_audit.'||label,NEW.task_id,NEW.worklog_id);
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.record_code_audit_event() FROM PUBLIC;
      CREATE TRIGGER code_audit_events AFTER INSERT OR UPDATE ON code_audits
        FOR EACH ROW EXECUTE FUNCTION hub.record_code_audit_event();

      CREATE FUNCTION hub.guard_code_audit_gate() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NEW.status IN ('testing','release_ready','completed') AND NEW.status<>OLD.status
          AND NOT hub.code_audit_ready(NEW.id) THEN
          RAISE EXCEPTION 'CODE_AUDIT_REQUIRED' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_gate() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guard_code_audit_gate() TO hub_runtime;
      CREATE TRIGGER code_audit_gate_guard BEFORE UPDATE OF status ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_gate();
    """)


def downgrade():
    raise RuntimeError("Retain code audit history and reports; restore a verified backup")
