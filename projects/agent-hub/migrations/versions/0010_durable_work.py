"""Immutable, repository-scoped code archives and server-verified Git receipts."""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE work_snapshots (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL REFERENCES tasks(id),
        run_id uuid NOT NULL REFERENCES task_runs(id), worklog_id uuid NOT NULL UNIQUE REFERENCES worklogs(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        evidence_hash varchar(64) NOT NULL, manifest_hash varchar(64) NOT NULL,
        archive_hash varchar(64) NOT NULL, storage_bytes integer NOT NULL CHECK(storage_bytes>0),
        receipt_hash varchar(64) NOT NULL, metadata jsonb NOT NULL
      );
      CREATE INDEX snapshots_task ON work_snapshots(project_id,task_id,created_at);
      CREATE TABLE code_deliveries (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL REFERENCES project_repositories(id),
        task_id uuid NOT NULL REFERENCES tasks(id), run_id uuid NOT NULL REFERENCES task_runs(id),
        worklog_id uuid NOT NULL REFERENCES worklogs(id), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), git_commit varchar(64) NOT NULL,
        git_ref varchar(200) NOT NULL, receipt_hash varchar(64) NOT NULL,
        UNIQUE(worklog_id,git_commit)
      );
      CREATE INDEX deliveries_task ON code_deliveries(project_id,task_id,created_at);
      CREATE FUNCTION hub.work_archive_usage(pid uuid) RETURNS TABLE(project_bytes bigint,total_bytes bigint)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT coalesce(sum(storage_bytes) FILTER(WHERE project_id=pid),0)::bigint,
          coalesce(sum(storage_bytes),0)::bigint FROM public.work_snapshots
          HAVING hub.can_access_project(pid)
      $$;
      REVOKE ALL ON FUNCTION hub.work_archive_usage(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.work_archive_usage(uuid) TO hub_runtime;
    """)
    for table in ("work_snapshots", "code_deliveries"):
        op.execute(f"""
          ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
          ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
          GRANT SELECT,INSERT ON {table} TO hub_runtime;
          CREATE POLICY evidence_owner ON {table} TO hub_owner USING(true) WITH CHECK(true);
          CREATE POLICY evidence_read ON {table} FOR SELECT TO hub_runtime USING(
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id={table}.worklog_id
              AND w.project_id={table}.project_id AND w.task_id={table}.task_id));
          CREATE POLICY evidence_insert ON {table} FOR INSERT TO hub_runtime WITH CHECK(
            hub.has_project_cap(project_id,'tasks:write') AND
            author_id=nullif(current_setting('hub.user_id',true),'')::uuid AND
            device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id={table}.worklog_id
              AND w.project_id={table}.project_id AND w.task_id={table}.task_id
              AND w.run_id={table}.run_id AND w.author_id={table}.author_id
              AND w.device_id={table}.device_id));
        """)


def downgrade():
    raise RuntimeError("Work recovery evidence must be preserved; restore application images without dropping archives")
