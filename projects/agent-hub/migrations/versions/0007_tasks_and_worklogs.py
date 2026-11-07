"""Atomic task ownership, fenced runs, immutable worklogs and workflow decisions."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION hub.has_project_cap(pid uuid, cap text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? cap
          ))
        $$;
        REVOKE ALL ON FUNCTION hub.has_project_cap(uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION hub.has_project_cap(uuid,text) TO hub_runtime;
        CREATE TABLE tasks (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), parent_id uuid,
          created_by uuid NOT NULL REFERENCES users(id), title varchar(200) NOT NULL,
          description text NOT NULL, acceptance text NOT NULL, scope jsonb NOT NULL,
          status varchar(24) NOT NULL DEFAULT 'draft', admission_step integer NOT NULL DEFAULT 0,
          version integer NOT NULL DEFAULT 1, lease_epoch integer NOT NULL DEFAULT 0,
          active_run_id uuid, lease_until timestamptz, submission_id uuid,
          UNIQUE(id,project_id), FOREIGN KEY(parent_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (status IN ('draft','ready','in_progress','review','testing','release_ready','completed')),
          CHECK (admission_step BETWEEN 0 AND 3 AND version>0 AND lease_epoch>=0),
          CHECK ((active_run_id IS NULL) = (lease_until IS NULL))
        );
        CREATE INDEX ix_tasks_project_created ON tasks(project_id,created_at,id);
        CREATE TABLE task_runs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
          user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          instance_id uuid NOT NULL, harness varchar(24) NOT NULL, role varchar(24) NOT NULL,
          lease_epoch integer NOT NULL, state varchar(24) NOT NULL DEFAULT 'active', ended_at timestamptz,
          UNIQUE(id,task_id,project_id), FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (role IN ('developer','reviewer','tester','publisher')),
          CHECK (state IN ('active','submitted','released','expired','finished'))
        );
        CREATE INDEX ix_task_runs_task ON task_runs(project_id,task_id,created_at);
        CREATE UNIQUE INDEX one_active_developer ON task_runs(task_id) WHERE role='developer' AND state='active';
        CREATE TABLE worklogs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, run_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          lease_epoch integer NOT NULL, summary varchar(300) NOT NULL, notes text NOT NULL,
          changed_paths jsonb NOT NULL, tests text NOT NULL, next_steps text NOT NULL,
          outcome varchar(24) NOT NULL, git_commit varchar(64), receipt_hash varchar(64) NOT NULL,
          FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
          CHECK (outcome IN ('checkpoint','blocked','failed'))
        );
        CREATE INDEX ix_worklogs_task ON worklogs(project_id,task_id,created_at,id);
        CREATE TABLE task_decisions (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          run_id uuid, worklog_id uuid, gate varchar(24) NOT NULL, approved boolean NOT NULL,
          evidence text NOT NULL, override_reason text NOT NULL, task_version integer NOT NULL,
          FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (gate IN ('requirements','design','feasibility','review','test','release'))
        );
        CREATE INDEX ix_task_decisions_task ON task_decisions(project_id,task_id,created_at);
        ALTER TABLE project_progress ADD COLUMN kind varchar(40) NOT NULL DEFAULT 'report';
        ALTER TABLE project_progress ADD COLUMN task_id uuid;
        ALTER TABLE project_progress ADD COLUMN run_id uuid;
        ALTER TABLE project_progress ADD COLUMN worklog_id uuid;
        GRANT SELECT,INSERT,UPDATE ON tasks,task_runs TO hub_runtime;
        GRANT SELECT,INSERT ON worklogs,task_decisions TO hub_runtime;
    """)
    for table in ("tasks", "task_runs", "worklogs", "task_decisions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tasks_read ON tasks FOR SELECT TO hub_runtime USING (hub.can_access_project(project_id));
        CREATE POLICY tasks_insert ON tasks FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:publish')
          AND created_by=nullif(current_setting('hub.user_id',true),'')::uuid);
        CREATE POLICY tasks_update ON tasks FOR UPDATE TO hub_runtime
          USING (hub.has_project_cap(project_id,'tasks:publish') OR hub.has_project_cap(project_id,'tasks:claim')
            OR hub.has_project_cap(project_id,'tasks:write') OR hub.has_project_cap(project_id,'tasks:review')
            OR hub.has_project_cap(project_id,'tasks:test'))
          WITH CHECK (hub.can_access_project(project_id));
        CREATE POLICY runs_read ON task_runs FOR SELECT TO hub_runtime USING (hub.can_access_project(project_id));
        CREATE POLICY runs_insert ON task_runs FOR INSERT TO hub_runtime WITH CHECK (
          user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND ((role='developer' AND hub.has_project_cap(project_id,'tasks:claim'))
            OR (role='reviewer' AND hub.has_project_cap(project_id,'tasks:review'))
            OR (role='tester' AND hub.has_project_cap(project_id,'tasks:test'))
            OR (role='publisher' AND hub.has_project_cap(project_id,'tasks:publish'))));
        CREATE POLICY runs_update ON task_runs FOR UPDATE TO hub_runtime
          USING (hub.has_project_cap(project_id,'tasks:claim') OR hub.has_project_cap(project_id,'tasks:write'))
          WITH CHECK (hub.can_access_project(project_id));
        CREATE POLICY logs_read ON worklogs FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
          OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY logs_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND EXISTS (SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.active_run_id
            WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id AND t.status='in_progress'
              AND t.lease_until>clock_timestamp() AND t.lease_epoch=worklogs.lease_epoch
              AND r.id=worklogs.run_id AND r.user_id=worklogs.author_id AND r.device_id=worklogs.device_id
              AND r.state='active'));
        CREATE POLICY decisions_read ON task_decisions FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
          OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY decisions_insert ON task_decisions FOR INSERT TO hub_runtime WITH CHECK (
          author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND ((gate='review' AND hub.has_project_cap(project_id,'tasks:review'))
            OR (gate='test' AND hub.has_project_cap(project_id,'tasks:test'))
            OR (gate IN ('requirements','design','feasibility','release') AND hub.has_project_cap(project_id,'tasks:publish'))));
        DROP POLICY progress_insert ON project_progress;
        CREATE POLICY progress_insert ON project_progress FOR INSERT WITH CHECK (
          author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND (hub.has_project_cap(project_id,'tasks:write')
            OR (kind<>'report' AND (hub.has_project_cap(project_id,'tasks:publish')
              OR hub.has_project_cap(project_id,'tasks:claim') OR hub.has_project_cap(project_id,'tasks:review')
              OR hub.has_project_cap(project_id,'tasks:test')))));
    """)


def downgrade():
    # Records are handoff evidence. Downgrading app images is safe; dropping these tables is not.
    raise RuntimeError("0007 is additive; preserve task history and roll back the application image only")
