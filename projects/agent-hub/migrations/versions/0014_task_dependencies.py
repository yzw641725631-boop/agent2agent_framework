"""Authorized task dependencies, including implicit parent completion edges."""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE task_dependencies (
        project_id uuid NOT NULL REFERENCES projects(id),
        task_id uuid NOT NULL, depends_on_id uuid NOT NULL,
        PRIMARY KEY(project_id,task_id,depends_on_id),
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        FOREIGN KEY(depends_on_id,project_id) REFERENCES tasks(id,project_id),
        CHECK(task_id<>depends_on_id)
      );
      CREATE INDEX ix_task_dependencies_source ON task_dependencies(project_id,depends_on_id);
      ALTER TABLE task_dependencies ENABLE ROW LEVEL SECURITY;
      ALTER TABLE task_dependencies FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT,DELETE ON task_dependencies TO hub_runtime;
      CREATE POLICY dependency_owner ON task_dependencies TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY dependency_read ON task_dependencies FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id)
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));
      CREATE POLICY dependency_insert ON task_dependencies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id
          AND t.lease_epoch=0 AND t.status IN ('draft','ready'))
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));
      -- Deleting an unseen edge must not silently change a restricted dependency.
      CREATE POLICY dependency_delete ON task_dependencies FOR DELETE TO hub_runtime USING(
        hub.has_project_cap(project_id,'tasks:publish')
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id
          AND t.lease_epoch=0 AND t.status IN ('draft','ready'))
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));

      -- Only return a gate Boolean for a visible task, never hidden IDs/titles/counts.
      CREATE FUNCTION hub.task_dependencies_satisfied(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND NOT EXISTS(
          SELECT 1 FROM public.task_dependencies d JOIN public.tasks t
            ON t.id=d.depends_on_id AND t.project_id=d.project_id
          WHERE d.project_id=pid AND d.task_id=tid AND t.status<>'completed')
      $$;
      CREATE FUNCTION hub.task_has_restricted_dependencies(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND EXISTS(
          SELECT 1 FROM public.task_dependencies d JOIN public.tasks t
            ON t.id=d.depends_on_id AND t.project_id=d.project_id
          WHERE d.project_id=pid AND d.task_id=tid AND NOT hub.can_read_scope(pid,t.repository_id))
      $$;
      CREATE FUNCTION hub.task_children_completed(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND NOT EXISTS(
          SELECT 1 FROM public.tasks t WHERE t.project_id=pid AND t.parent_id=tid AND t.status<>'completed')
      $$;
      CREATE FUNCTION hub.task_dependency_cycle(pid uuid, tid uuid, deps uuid[]) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH RECURSIVE walk(id) AS (
          SELECT unnest(deps)
          UNION
          SELECT next.id FROM walk w CROSS JOIN LATERAL (
            SELECT d.depends_on_id AS id FROM public.task_dependencies d
              WHERE d.project_id=pid AND d.task_id=w.id
            UNION
            SELECT t.id FROM public.tasks t WHERE t.project_id=pid AND t.parent_id=w.id
          ) next
        )
        SELECT hub.has_project_cap(pid,'tasks:publish') AND EXISTS(
          SELECT 1 FROM public.tasks t WHERE t.project_id=pid AND t.id=tid
            AND hub.can_read_scope(pid,t.repository_id)) AND EXISTS(SELECT 1 FROM walk WHERE id=tid)
      $$;
      REVOKE ALL ON FUNCTION hub.task_dependencies_satisfied(uuid,uuid),hub.task_children_completed(uuid,uuid),
        hub.task_has_restricted_dependencies(uuid,uuid),hub.task_dependency_cycle(uuid,uuid,uuid[]) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.task_dependencies_satisfied(uuid,uuid),hub.task_children_completed(uuid,uuid),
        hub.task_has_restricted_dependencies(uuid,uuid),hub.task_dependency_cycle(uuid,uuid,uuid[]) TO hub_runtime;
    """)


def downgrade():
    raise RuntimeError("Preserve task coordination history; restore a verified backup instead")
