"""Device-bound upload-only closeouts; normal authorization is never restored."""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE closeout_jobs (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), reason varchar(40) NOT NULL,
        scope jsonb NOT NULL, token_digest varchar(64) NOT NULL, expires_at timestamptz NOT NULL,
        state varchar(24) NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','uploaded','conflicts','complete')),
        seal_id uuid, cleanup_report jsonb
      );
      CREATE INDEX ix_closeout_jobs_project_id ON closeout_jobs(project_id);
      CREATE INDEX ix_closeout_jobs_device_id ON closeout_jobs(device_id,created_at);
      CREATE TABLE closeout_runs (
        job_id uuid NOT NULL REFERENCES closeout_jobs(id), run_id uuid NOT NULL REFERENCES task_runs(id),
        task_id uuid NOT NULL REFERENCES tasks(id), repository_id uuid REFERENCES project_repositories(id),
        lease_epoch integer NOT NULL, context_id uuid, payload jsonb NOT NULL,
        PRIMARY KEY(job_id,run_id)
      );
      CREATE TABLE closeout_seals (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES closeout_jobs(id), inventory jsonb NOT NULL, receipt_hash varchar(64) NOT NULL
      );
      CREATE INDEX ix_closeout_seals_job_id ON closeout_seals(job_id);
      ALTER TABLE closeout_jobs ADD FOREIGN KEY(seal_id) REFERENCES closeout_seals(id);
      ALTER TABLE worklogs ADD COLUMN closeout_id uuid REFERENCES closeout_jobs(id);
      CREATE INDEX ix_worklogs_closeout ON worklogs(closeout_id) WHERE closeout_id IS NOT NULL;
      GRANT SELECT,INSERT ON closeout_jobs,closeout_seals TO hub_runtime;
      GRANT UPDATE(state,seal_id,cleanup_report,expires_at) ON closeout_jobs TO hub_runtime;
      GRANT SELECT ON closeout_runs TO hub_runtime;
    """)
    for table in ("closeout_jobs", "closeout_runs", "closeout_seals"):
        op.execute(f"""
          ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
          ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
          CREATE POLICY closeout_owner ON {table} TO hub_owner USING(true) WITH CHECK(true);
        """)
    # Definer functions only receive the original frozen run metadata, never an elevated actor.
    for table in ("tasks", "task_runs", "work_contexts"):
        op.execute(f"CREATE POLICY closeout_owner_read ON {table} FOR SELECT TO hub_owner USING(true)")
    for table in ("tasks", "task_runs"):
        op.execute(f"CREATE POLICY closeout_owner_release ON {table} FOR UPDATE TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE FUNCTION hub.closeout_authenticated(cid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.closeout_jobs c WHERE c.id=cid
          AND c.id=nullif(current_setting('hub.closeout_id',true),'')::uuid
          AND c.token_digest=current_setting('hub.closeout_digest',true)
          AND c.device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND c.user_id=nullif(current_setting('hub.user_id',true),'')::uuid)
      $$;
      CREATE FUNCTION hub.closeout_run_allowed(cid uuid,pid uuid,tid uuid,rid uuid,uid uuid,did uuid)
      RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.closeout_authenticated(cid) AND EXISTS(
          SELECT 1 FROM public.closeout_jobs c JOIN public.closeout_runs r ON r.job_id=c.id
          WHERE c.id=cid AND c.project_id=pid AND c.user_id=uid AND c.device_id=did
            AND c.state<>'complete' AND c.expires_at>clock_timestamp() AND r.run_id=rid AND r.task_id=tid)
      $$;
      CREATE FUNCTION hub.closeout_discover(thumb text) RETURNS SETOF public.closeout_jobs
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT c.* FROM public.closeout_jobs c JOIN public.devices d ON d.id=c.device_id
          WHERE d.thumbprint=thumb ORDER BY c.created_at,c.id
      $$;
      CREATE FUNCTION hub.closeout_freeze(cid uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE c public.closeout_jobs;
      BEGIN
        SELECT * INTO c FROM public.closeout_jobs WHERE id=cid;
        IF NOT FOUND OR NOT hub.has_project_cap(c.project_id,'members:manage') THEN
          RAISE EXCEPTION 'permission denied';
        END IF;
        INSERT INTO public.closeout_runs(job_id,run_id,task_id,repository_id,lease_epoch,context_id,payload)
          SELECT c.id,r.id,r.task_id,t.repository_id,r.lease_epoch,w.id,
            coalesce(w.payload,jsonb_build_object('task',jsonb_build_object('scope',t.scope),'policy',
              jsonb_build_object('protected_paths','[]'::jsonb)))
          FROM public.task_runs r JOIN public.tasks t ON t.id=r.task_id AND t.project_id=r.project_id
          LEFT JOIN LATERAL (SELECT x.id,x.payload FROM public.work_contexts x
            WHERE x.run_id=r.id AND x.author_id=c.user_id AND x.device_id=c.device_id
              AND x.acknowledged_at IS NOT NULL AND x.created_at<=c.created_at
            ORDER BY x.created_at DESC,x.id DESC LIMIT 1) w ON true
          WHERE r.project_id=c.project_id AND r.user_id=c.user_id AND r.device_id=c.device_id
            AND r.role='developer' AND r.created_at<=c.created_at
            AND (c.scope->'repository_ids'='null'::jsonb OR
              c.scope->'repository_ids' @> to_jsonb(t.repository_id::text));
        WITH ended AS (
          UPDATE public.task_runs r SET state='released',ended_at=clock_timestamp()
            WHERE r.state='active' AND r.role='developer'
              AND EXISTS(SELECT 1 FROM public.closeout_runs f WHERE f.job_id=c.id AND f.run_id=r.id)
            RETURNING r.id)
          UPDATE public.tasks t SET active_run_id=NULL,lease_until=NULL,status='ready',
            lease_epoch=lease_epoch+1,version=version+1
          WHERE t.status='in_progress' AND t.active_run_id IN (SELECT id FROM ended);
      END $$;
      CREATE FUNCTION hub.closeout_context(cid uuid,rid uuid,wid uuid) RETURNS jsonb
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT w.payload FROM public.work_contexts w JOIN public.closeout_jobs c ON c.id=cid
        JOIN public.closeout_runs r ON r.job_id=c.id AND r.run_id=rid
        WHERE hub.closeout_authenticated(cid) AND w.id=wid AND w.run_id=rid
          AND w.author_id=c.user_id AND w.device_id=c.device_id AND w.created_at<=c.created_at
          AND w.acknowledged_at<=c.created_at
      $$;
      REVOKE ALL ON FUNCTION hub.closeout_authenticated(uuid),hub.closeout_run_allowed(uuid,uuid,uuid,uuid,uuid,uuid),
        hub.closeout_discover(text),hub.closeout_freeze(uuid),hub.closeout_context(uuid,uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.closeout_authenticated(uuid),hub.closeout_run_allowed(uuid,uuid,uuid,uuid,uuid,uuid),
        hub.closeout_discover(text),hub.closeout_freeze(uuid),hub.closeout_context(uuid,uuid,uuid) TO hub_runtime;
      CREATE POLICY closeout_admin_read ON closeout_jobs FOR SELECT TO hub_runtime USING(
        hub.has_project_cap(project_id,'members:manage'));
      CREATE POLICY closeout_device_read ON closeout_jobs FOR SELECT TO hub_runtime USING(hub.closeout_authenticated(id));
      CREATE POLICY closeout_create ON closeout_jobs FOR INSERT TO hub_runtime
        WITH CHECK(hub.has_project_cap(project_id,'members:manage'));
      CREATE POLICY closeout_update ON closeout_jobs FOR UPDATE TO hub_runtime
        USING(hub.closeout_authenticated(id) OR hub.is_superadmin())
        WITH CHECK(hub.closeout_authenticated(id) OR hub.is_superadmin());
      CREATE POLICY closeout_run_read ON closeout_runs FOR SELECT TO hub_runtime
        USING(hub.closeout_authenticated(job_id));
      CREATE POLICY closeout_seal_read ON closeout_seals FOR SELECT TO hub_runtime
        USING(hub.closeout_authenticated(job_id) OR EXISTS(SELECT 1 FROM closeout_jobs c WHERE c.id=job_id));
      CREATE POLICY closeout_seal_insert ON closeout_seals FOR INSERT TO hub_runtime
        WITH CHECK(hub.closeout_authenticated(job_id));
      CREATE POLICY closeout_log_read ON worklogs FOR SELECT TO hub_runtime USING(
        hub.closeout_authenticated(closeout_id));
      CREATE POLICY closeout_log_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK(
        hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      CREATE POLICY closeout_log_scope ON worklogs AS RESTRICTIVE FOR INSERT TO hub_runtime WITH CHECK(
        closeout_id IS NULL OR hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      DROP POLICY inherited_repository_scope ON worklogs;
      CREATE POLICY inherited_repository_scope ON worklogs AS RESTRICTIVE TO hub_runtime
        USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id)
          OR hub.closeout_authenticated(closeout_id))
        WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id)
          OR hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      CREATE POLICY closeout_snapshot_insert ON work_snapshots FOR INSERT TO hub_runtime WITH CHECK(
        EXISTS(SELECT 1 FROM worklogs w WHERE w.id=worklog_id AND w.project_id=work_snapshots.project_id
          AND w.task_id=work_snapshots.task_id AND w.run_id=work_snapshots.run_id
          AND hub.closeout_run_allowed(w.closeout_id,w.project_id,w.task_id,w.run_id,work_snapshots.author_id,work_snapshots.device_id)));
      CREATE OR REPLACE FUNCTION hub.work_archive_usage(pid uuid) RETURNS TABLE(project_bytes bigint,total_bytes bigint)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT coalesce(sum(storage_bytes) FILTER(WHERE project_id=pid),0)::bigint,
          coalesce(sum(storage_bytes),0)::bigint FROM public.work_snapshots
          HAVING hub.can_access_project(pid) OR EXISTS(SELECT 1 FROM public.closeout_jobs c
            WHERE c.project_id=pid AND hub.closeout_authenticated(c.id) AND c.state<>'complete'
              AND c.expires_at>clock_timestamp())
      $$;
    """)


def downgrade():
    raise RuntimeError("Preserve closeout jobs and handoff receipts; roll back application images only")
