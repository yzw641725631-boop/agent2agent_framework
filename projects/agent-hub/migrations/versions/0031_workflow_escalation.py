"""Durable workflow budgets, bounded watchdog and explicit human recovery."""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
      ALTER TABLE tasks ADD COLUMN workflow_runtime jsonb NOT NULL DEFAULT '{}';
      CREATE TABLE task_workflow_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
        cycle integer NOT NULL, revision integer NOT NULL, kind varchar(32) NOT NULL,
        reason varchar(40) NOT NULL, snapshot jsonb NOT NULL,
        author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        resolution text NOT NULL DEFAULT '',
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        UNIQUE(task_id,revision),
        CHECK(kind IN ('manual_required','resumed')),
        CHECK((author_id IS NULL)=(device_id IS NULL))
      );
      CREATE INDEX workflow_history ON task_workflow_events(project_id,task_id,created_at,id);
      ALTER TABLE task_workflow_events ENABLE ROW LEVEL SECURITY;
      ALTER TABLE task_workflow_events FORCE ROW LEVEL SECURITY;
      CREATE POLICY workflow_owner ON task_workflow_events TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY workflow_read ON task_workflow_events FOR SELECT TO hub_runtime USING(
        (hub.has_project_cap(project_id,'logs:read') OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid)
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_workflow_events.project_id));
      GRANT SELECT ON task_workflow_events TO hub_runtime;
      ALTER TABLE project_progress ALTER COLUMN author_id DROP NOT NULL;
      ALTER TABLE project_progress ALTER COLUMN device_id DROP NOT NULL;
      ALTER TABLE project_progress ADD CONSTRAINT progress_system_identity CHECK(
        (author_id IS NOT NULL AND device_id IS NOT NULL) OR
        (author_id IS NULL AND device_id IS NULL AND kind='task.manual_required'));

      -- Runtime may change business columns, never its counters, deadlines or recovery cycle.
      REVOKE UPDATE ON tasks FROM hub_runtime;
      GRANT UPDATE(project_id,repository_id,parent_id,created_by,title,description,acceptance,scope,
        execution_mode,execution_role,role_profile,workflow,workflow_reason,workflow_policy,documentation,
        status,admission_step,version,lease_epoch,active_run_id,lease_until,submission_id) ON tasks TO hub_runtime;

      CREATE FUNCTION hub.workflow_stage(t public.tasks) RETURNS text
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
        SELECT CASE t.status WHEN 'draft' THEN coalesce(
          (t.workflow_policy->'admission'->>t.admission_step),
          (ARRAY['requirements','design','feasibility'])[t.admission_step+1], 'requirements')
          WHEN 'in_progress' THEN 'development' WHEN 'ready' THEN 'queued'
          WHEN 'review' THEN 'review' WHEN 'testing' THEN 'test'
          WHEN 'release_ready' THEN 'release' ELSE 'completed' END
      $$;
      CREATE FUNCTION hub.workflow_due(r jsonb) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT coalesce(r->>'state'='active' AND (r->>'deadline_at')::timestamptz<=statement_timestamp(),false)
      $$;
      CREATE FUNCTION hub.workflow_lock(pid uuid) RETURNS void
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        -- Never wait for a project lock while a legacy SQL caller already holds a row lock.
        IF NOT pg_try_advisory_xact_lock_shared(7206110501) OR NOT pg_try_advisory_xact_lock(
          ('x'||substr(encode(sha256(convert_to('project:'||pid::text,'UTF8')),'hex'),1,16))::bit(64)::bigint)
          THEN RAISE EXCEPTION 'workflow project busy' USING ERRCODE='55P03'; END IF;
      END $$;
      CREATE FUNCTION hub.workflow_event(t public.tasks, k text, why text, uid uuid DEFAULT NULL,
        did uuid DEFAULT NULL, note text DEFAULT '') RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        INSERT INTO public.task_workflow_events(project_id,task_id,cycle,revision,kind,reason,snapshot,
          author_id,device_id,resolution) VALUES(t.project_id,t.id,(t.workflow_runtime->>'cycle')::int,
          (t.workflow_runtime->>'revision')::int,k,why,t.workflow_runtime,uid,did,note);
        INSERT INTO public.project_progress(project_id,repository_id,author_id,device_id,status,summary,
          detail,task_reference,kind,task_id) VALUES(t.project_id,t.repository_id,uid,did,'checkpoint',
          left(CASE k WHEN 'manual_required' THEN '任务已转人工处理 · ' ELSE '负责人已恢复流程 · ' END||t.title,300),
          '',t.id::text,CASE k WHEN 'manual_required' THEN 'task.manual_required' ELSE 'task.workflow_resumed' END,t.id);
        INSERT INTO public.audit_events(id,project_id,repository_id,actor_id,action,object_id,request_id,details)
          VALUES(gen_random_uuid(),t.project_id,t.repository_id,uid,'task.workflow_'||k,t.id::text,
          'workflow:'||t.id::text,jsonb_build_object('cycle',t.workflow_runtime->'cycle',
          'revision',t.workflow_runtime->'revision','reason',why));
      END $$;
      CREATE FUNCTION hub.pause_task_workflow(pid uuid, tid uuid, why text) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb;
      BEGIN
        PERFORM hub.workflow_lock(pid);
        SELECT * INTO t FROM public.tasks WHERE id=tid AND project_id=pid FOR UPDATE;
        r:=t.workflow_runtime;
        IF r->>'state'<>'active' THEN RETURN r; END IF;
        r:=r||jsonb_build_object('state','manual','reason',why,'paused_at',statement_timestamp(),
          'revision',(r->>'revision')::int+1);
        UPDATE public.tasks SET workflow_runtime=r,version=version+1 WHERE id=tid RETURNING * INTO t;
        PERFORM hub.workflow_event(t,'manual_required',why);
        RETURN r;
      END $$;
      CREATE FUNCTION hub.guard_task_workflow() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb; stage text; limits jsonb; config integer;
      BEGIN
        PERFORM hub.workflow_lock(NEW.project_id);
        IF TG_OP='INSERT' THEN
          SELECT content->'workflow_limits',version INTO limits,config FROM public.project_collaboration
            WHERE project_id=NEW.project_id ORDER BY version DESC LIMIT 1;
          limits:=jsonb_build_object('stage_rejections',3,'test_returns',2,'stage_timeout_seconds',86400)||coalesce(limits,'{}');
          IF coalesce(jsonb_typeof(limits)<>'object' OR (limits->>'stage_rejections')::int NOT BETWEEN 1 AND 10
            OR (limits->>'test_returns')::int NOT BETWEEN 1 AND 10
            OR (limits->>'stage_timeout_seconds')::int NOT BETWEEN 60 AND 604800,true)
            THEN RAISE EXCEPTION 'invalid workflow limits'; END IF;
          r:=jsonb_build_object('state','active','cycle',1,'revision',1,'rejections','{}'::jsonb,
            'test_returns',0,'limits',limits,'configuration_version',coalesce(config,0),'stage','');
        ELSE
          -- Only the owner functions can write this column (no runtime column privilege).
          IF NEW.workflow_runtime IS DISTINCT FROM OLD.workflow_runtime THEN RETURN NEW; END IF;
          r:=OLD.workflow_runtime;
          IF r->>'state'='manual' OR hub.workflow_due(r) THEN
            RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000';
          END IF;
        END IF;
        stage:=hub.workflow_stage(NEW);
        IF stage='queued' AND r->>'stage'='development' THEN stage:='development'; END IF;
        IF r->>'stage' IS DISTINCT FROM stage THEN
          r:=r||jsonb_build_object('stage',stage,'started_at',statement_timestamp(),
            'deadline_at',CASE WHEN stage IN ('queued','completed') THEN NULL ELSE
              statement_timestamp()+make_interval(secs=>(r->'limits'->>'stage_timeout_seconds')::int) END);
        END IF;
        IF stage='completed' THEN r:=r||'{"state":"completed"}'; END IF;
        NEW.workflow_runtime:=r;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_guard BEFORE INSERT OR UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.guard_task_workflow();
      CREATE INDEX workflow_deadlines ON tasks(((workflow_runtime->>'deadline_at')))
        WHERE workflow_runtime->>'state'='active' AND workflow_runtime->>'deadline_at' IS NOT NULL;

      -- Start a fresh timing baseline on upgrade. Historical failures remain in decisions.
      UPDATE tasks SET workflow_runtime=jsonb_build_object('state',CASE WHEN status='completed' THEN 'completed' ELSE 'active' END,
        'cycle',1,'revision',1,'rejections','{}'::jsonb,'test_returns',0,
        'limits',jsonb_build_object('stage_rejections',3,'test_returns',2,'stage_timeout_seconds',86400),
        'configuration_version',0,'stage',hub.workflow_stage(tasks),'started_at',statement_timestamp(),
        'deadline_at',CASE WHEN status IN ('ready','completed') THEN NULL ELSE statement_timestamp()+interval '24 hours' END);

      CREATE FUNCTION hub.count_task_rejection() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb; n integer; loops integer; why text;
      BEGIN
        PERFORM hub.workflow_lock(NEW.project_id);
        SELECT * INTO t FROM public.tasks WHERE id=NEW.task_id AND project_id=NEW.project_id FOR UPDATE;
        r:=t.workflow_runtime;
        IF r->>'state'='manual' OR hub.workflow_due(r) THEN
          RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000'; END IF;
        IF NEW.approved THEN RETURN NEW; END IF;
        n:=coalesce((r->'rejections'->>NEW.gate)::int,0)+1;
        loops:=(r->>'test_returns')::int+CASE WHEN NEW.gate='test' THEN 1 ELSE 0 END;
        r:=jsonb_set(r,ARRAY['rejections',NEW.gate],to_jsonb(n))||jsonb_build_object('test_returns',loops);
        UPDATE public.tasks SET workflow_runtime=r WHERE id=t.id;
        why:=CASE WHEN loops>=(r->'limits'->>'test_returns')::int THEN 'test_returns'
          WHEN n>=(r->'limits'->>'stage_rejections')::int THEN 'stage_rejections' END;
        IF why IS NOT NULL THEN PERFORM hub.pause_task_workflow(t.project_id,t.id,why); END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_decision AFTER INSERT ON task_decisions
        FOR EACH ROW EXECUTE FUNCTION hub.count_task_rejection();
      CREATE FUNCTION hub.guard_workflow_run() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb;
      BEGIN
        IF NEW.state='active' THEN
          PERFORM hub.workflow_lock(NEW.project_id);
          SELECT workflow_runtime INTO r FROM public.tasks WHERE id=NEW.task_id AND project_id=NEW.project_id;
          IF r->>'state'='manual' OR hub.workflow_due(r) THEN
            RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000'; END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_run_guard BEFORE INSERT OR UPDATE ON task_runs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_workflow_run();

      -- Revocation must still be able to end a paused lease. Only this owner function
      -- can stamp runtime; normal callers have no UPDATE privilege on that column.
      DO $$ DECLARE definition text; changed text; BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        changed:=replace(definition,'UPDATE public.tasks t SET active_run_id=NULL',
          'UPDATE public.tasks t SET workflow_runtime=t.workflow_runtime||jsonb_build_object(''revoked_run'',t.active_run_id),active_run_id=NULL');
        IF changed=definition THEN RAISE EXCEPTION 'closeout freeze shape changed'; END IF;
        EXECUTE changed;
      END $$;
      CREATE FUNCTION hub.check_task_workflow(pid uuid,tid uuid) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb;
      BEGIN
        IF NOT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) THEN RETURN NULL; END IF;
        PERFORM hub.workflow_lock(pid);
        SELECT workflow_runtime INTO r FROM public.tasks WHERE id=tid AND project_id=pid;
        IF hub.workflow_due(r) THEN r:=hub.pause_task_workflow(pid,tid,'stage_timeout'); END IF;
        RETURN r;
      END $$;
      CREATE FUNCTION hub.resume_task_workflow(pid uuid,tid uuid,v integer,rev integer,note text) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb; uid uuid; did uuid; stage text;
      BEGIN
        IF NOT hub.has_project_cap(pid,'tasks:publish') THEN RETURN false; END IF;
        uid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did:=nullif(current_setting('hub.device_id',true),'')::uuid;
        IF uid IS NULL OR did IS NULL OR length(trim(note)) NOT BETWEEN 10 AND 2000 THEN RETURN false; END IF;
        PERFORM hub.workflow_lock(pid);
        SELECT * INTO t FROM public.tasks WHERE id=tid AND project_id=pid FOR UPDATE;
        IF NOT FOUND OR NOT hub.can_read_scope(pid,t.repository_id) OR t.version<>v
          OR (t.workflow_runtime->>'revision')::int<>rev OR t.workflow_runtime->>'state'<>'manual' THEN RETURN false; END IF;
        IF t.active_run_id IS NOT NULL THEN
          UPDATE public.task_runs SET state='expired',ended_at=statement_timestamp()
            WHERE id=t.active_run_id AND state='active';
        END IF;
        IF t.status='in_progress' THEN t.status:='ready'; END IF;
        stage:=hub.workflow_stage(t);
        r:=t.workflow_runtime||jsonb_build_object('state','active','cycle',(t.workflow_runtime->>'cycle')::int+1,
          'revision',(t.workflow_runtime->>'revision')::int+1,'reason',NULL,'paused_at',NULL,
          'rejections','{}'::jsonb,'test_returns',0,'stage',stage,'started_at',statement_timestamp(),
          'deadline_at',CASE WHEN stage IN ('queued','completed') THEN NULL ELSE
            statement_timestamp()+make_interval(secs=>(t.workflow_runtime->'limits'->>'stage_timeout_seconds')::int) END);
        UPDATE public.tasks SET workflow_runtime=r,status=t.status,active_run_id=NULL,lease_until=NULL,
          version=version+1 WHERE id=tid RETURNING * INTO t;
        PERFORM hub.workflow_event(t,'resumed','human_resolution',uid,did,trim(note));
        RETURN true;
      END $$;
      CREATE FUNCTION hub.expire_task_workflows(batch integer DEFAULT 100) RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE item record; count integer:=0;
      BEGIN
        IF NOT pg_try_advisory_xact_lock_shared(7206110501) THEN RETURN 0; END IF;
        FOR item IN SELECT id,project_id FROM public.tasks WHERE workflow_runtime->>'state'='active'
          AND (workflow_runtime->>'deadline_at')::timestamptz<=statement_timestamp()
          ORDER BY workflow_runtime->>'deadline_at',id LIMIT least(greatest(batch,1),100)*5 LOOP
          IF pg_try_advisory_xact_lock(('x'||substr(encode(sha256(convert_to(
            'project:'||item.project_id::text,'UTF8')),'hex'),1,16))::bit(64)::bigint) THEN
            PERFORM hub.pause_task_workflow(item.project_id,item.id,'stage_timeout'); count:=count+1;
          END IF;
          EXIT WHEN count>=least(greatest(batch,1),100);
        END LOOP;
        RETURN count;
      END $$;
      CREATE FUNCTION hub.workflow_preservation(pid uuid,tid uuid,rid uuid,epoch integer,uid uuid,did uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t JOIN public.task_runs r ON r.id=t.active_run_id
          WHERE t.project_id=pid AND t.id=tid AND t.status='in_progress' AND t.active_run_id=rid
            AND t.lease_epoch=epoch AND r.user_id=uid AND r.device_id=did AND r.state='active'
            AND (t.workflow_runtime->>'state'='manual' OR hub.workflow_due(t.workflow_runtime))
            AND uid=nullif(current_setting('hub.user_id',true),'')::uuid
            AND did=nullif(current_setting('hub.device_id',true),'')::uuid
            AND hub.has_project_cap(pid,'tasks:write') AND hub.can_read_scope(pid,t.repository_id)
            AND (t.repository_id IS NULL OR hub.has_repository_cap(pid,t.repository_id,'code:write')))
      $$;
      DROP POLICY logs_insert ON worklogs;
      CREATE POLICY logs_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND (EXISTS(SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.active_run_id
          WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id AND t.status='in_progress'
            AND t.lease_until>clock_timestamp() AND t.lease_epoch=worklogs.lease_epoch
            AND r.id=worklogs.run_id AND r.user_id=worklogs.author_id AND r.device_id=worklogs.device_id AND r.state='active')
          OR hub.workflow_preservation(project_id,task_id,run_id,lease_epoch,author_id,device_id)));
      REVOKE ALL ON FUNCTION hub.workflow_stage(public.tasks),hub.workflow_due(jsonb),hub.workflow_lock(uuid),
        hub.workflow_event(public.tasks,text,text,uuid,uuid,text),hub.pause_task_workflow(uuid,uuid,text),
        hub.guard_task_workflow(),hub.guard_workflow_run(),hub.count_task_rejection(),hub.check_task_workflow(uuid,uuid),
        hub.resume_task_workflow(uuid,uuid,integer,integer,text),hub.expire_task_workflows(integer),
        hub.workflow_preservation(uuid,uuid,uuid,integer,uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.check_task_workflow(uuid,uuid),
        hub.resume_task_workflow(uuid,uuid,integer,integer,text),
        hub.workflow_preservation(uuid,uuid,uuid,integer,uuid,uuid) TO hub_runtime;
      GRANT EXECUTE ON FUNCTION hub.expire_task_workflows(integer) TO hub_memory;
    """)


def downgrade():
    raise RuntimeError("Preserve workflow escalation and human recovery history; roll back application images only")
