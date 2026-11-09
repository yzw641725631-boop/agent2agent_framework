"""Bind occupational roles to tasks without granting new permissions."""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE tasks ADD COLUMN execution_role varchar(24) NOT NULL DEFAULT 'developer'
        CHECK(execution_role IN ('project-manager','architect','designer','developer'));
      ALTER TABLE task_runs DROP CONSTRAINT task_runs_role_check;
      ALTER TABLE task_runs ADD CONSTRAINT task_runs_role_check
        CHECK(role IN ('project-manager','architect','designer','developer','reviewer','tester','publisher'));
      DROP INDEX one_active_developer;
      CREATE UNIQUE INDEX one_active_primary_run ON task_runs(task_id) WHERE state='active';
      DROP POLICY runs_insert ON task_runs;
      CREATE POLICY runs_insert ON task_runs FOR INSERT TO hub_runtime WITH CHECK (
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND ((state='active' AND role IN ('project-manager','architect','designer','developer')
          AND hub.has_project_cap(project_id,'tasks:claim') AND hub.has_project_cap(project_id,'tasks:write')
          AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_runs.project_id AND t.execution_role=task_runs.role))
        OR (state='finished' AND (
          (role IN ('project-manager','architect','publisher') AND hub.has_project_cap(project_id,'tasks:publish'))
          OR (role='reviewer' AND hub.has_project_cap(project_id,'tasks:review'))
          OR (role='tester' AND hub.has_project_cap(project_id,'tasks:test'))))));
      -- Preserve revocation capture for every primary role, including observed design work.
      DO $migration$
      DECLARE definition text;
      BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,'r.role=''developer''','')) <> 2*length('r.role=''developer''') THEN
          RAISE EXCEPTION 'Unexpected closeout function; verify primary-role capture before migration';
        END IF;
        definition := replace(definition,'r.role=''developer''',
          'r.role IN (''project-manager'',''architect'',''designer'',''developer'')');
        EXECUTE definition;
      END $migration$;
    """)


def downgrade():
    raise RuntimeError("Preserve recorded occupational roles; restore only from a verified backup")
