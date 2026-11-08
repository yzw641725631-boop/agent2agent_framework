"""Independent repositories, approved resource grants and inherited visibility."""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE projects ADD COLUMN repository_mode varchar(16) NOT NULL DEFAULT 'legacy'
        CHECK(repository_mode IN ('legacy','scoped'));
      ALTER TABLE access_grants ADD COLUMN repository_permissions jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(repository_permissions)='array');
      CREATE TABLE project_repositories (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), slug varchar(80) NOT NULL,
        name varchar(200) NOT NULL, platform varchar(24) NOT NULL,
        repository_url varchar(400) NOT NULL, branch varchar(200) NOT NULL,
        auth_mode varchar(24) NOT NULL CHECK(auth_mode IN ('public','token','github_app')),
        encrypted_token text, installation_id bigint,
        status varchar(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0),
        UNIQUE(project_id,slug), UNIQUE(id,project_id), UNIQUE(project_id,repository_url)
      );
      CREATE INDEX ix_project_repositories_project_id ON project_repositories(project_id);
      ALTER TABLE project_repositories ENABLE ROW LEVEL SECURITY;
      ALTER TABLE project_repositories FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT,UPDATE ON project_repositories TO hub_runtime;
      GRANT SELECT ON project_repositories TO hub_indexer;
      -- Definer lookups see metadata without recursively invoking the runtime policy.
      CREATE POLICY repository_owner ON project_repositories TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY repository_indexer ON project_repositories FOR SELECT TO hub_indexer USING(true);
      CREATE FUNCTION hub.has_repository_cap(pid uuid, rid uuid, cap text) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND EXISTS (
          SELECT 1 FROM public.project_repositories r WHERE r.id=rid AND r.project_id=pid
          AND r.status='active' AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id',true),'')::uuid AND g.project_id=pid
            AND g.repository_permissions @> jsonb_build_array(jsonb_build_object(
              'repository_id',rid::text,'capabilities',jsonb_build_array(cap)))
          ))
        )
      $$;
      CREATE FUNCTION hub.can_read_scope(pid uuid, rid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR
          CASE WHEN rid IS NOT NULL THEN hub.has_repository_cap(pid,rid,'code:read')
          ELSE EXISTS(SELECT 1 FROM public.projects p WHERE p.id=pid AND p.repository_mode='legacy') END)
      $$;
      CREATE FUNCTION hub.can_read_source(pid uuid, sid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR
          CASE WHEN sid=pid THEN hub.can_read_scope(pid,NULL)
          ELSE hub.has_repository_cap(pid,sid,'documents:read') OR
            (hub.can_manage_documents(pid) AND hub.has_repository_cap(pid,sid,'code:read')) END)
      $$;
      REVOKE ALL ON FUNCTION hub.has_repository_cap(uuid,uuid,text), hub.can_read_scope(uuid,uuid),
        hub.can_read_source(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.has_repository_cap(uuid,uuid,text), hub.can_read_scope(uuid,uuid),
        hub.can_read_source(uuid,uuid), hub.has_project_cap(uuid,text) TO hub_runtime,hub_indexer;
      CREATE POLICY repository_read ON project_repositories FOR SELECT TO hub_runtime USING (
        hub.has_project_cap(project_id,'repositories:manage') OR
        hub.has_project_cap(project_id,'grants:request') OR
        hub.has_repository_cap(project_id,id,'code:read') OR
        hub.has_repository_cap(project_id,id,'documents:read'));
      CREATE POLICY repository_insert ON project_repositories FOR INSERT TO hub_runtime
        WITH CHECK(hub.has_project_cap(project_id,'repositories:manage'));
      CREATE POLICY repository_update ON project_repositories FOR UPDATE TO hub_runtime
        USING(hub.has_project_cap(project_id,'repositories:manage'))
        WITH CHECK(hub.has_project_cap(project_id,'repositories:manage'));
      -- Mode activation is a one-way operation; runtime cannot arbitrarily update projects.
      CREATE FUNCTION hub.activate_repository_scope(pid uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NOT hub.has_project_cap(pid,'repositories:manage') THEN RAISE EXCEPTION 'permission denied'; END IF;
        UPDATE public.projects SET repository_mode='scoped', version=version+1
          WHERE id=pid AND repository_mode='legacy';
      END $$;
      REVOKE ALL ON FUNCTION hub.activate_repository_scope(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.activate_repository_scope(uuid) TO hub_runtime;
      CREATE FUNCTION hub.github_app_configuration() RETURNS TABLE(encrypted_value text, version integer)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT s.encrypted_value,s.version FROM public.site_configuration s WHERE s.key='github-app'
      $$;
      REVOKE ALL ON FUNCTION hub.github_app_configuration() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.github_app_configuration() TO hub_runtime,hub_indexer;
      CREATE POLICY project_scope_activation ON projects FOR UPDATE TO hub_owner
        USING(hub.has_project_cap(id,'repositories:manage')) WITH CHECK(hub.can_access_project(id));
      ALTER TABLE tasks ADD COLUMN repository_id uuid;
      ALTER TABLE tasks ADD CONSTRAINT task_repository_project FOREIGN KEY(repository_id,project_id)
        REFERENCES project_repositories(id,project_id);
      CREATE INDEX ix_tasks_repository ON tasks(project_id,repository_id,created_at);
      CREATE POLICY task_repository_scope ON tasks AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      ALTER TABLE project_progress ADD COLUMN repository_id uuid;
      ALTER TABLE project_progress ADD CONSTRAINT progress_repository_project FOREIGN KEY(repository_id,project_id)
        REFERENCES project_repositories(id,project_id);
      ALTER TABLE audit_events ADD COLUMN repository_id uuid REFERENCES project_repositories(id);
      CREATE POLICY progress_repository_scope ON project_progress AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY audit_repository_scope ON audit_events AS RESTRICTIVE FOR SELECT TO hub_runtime
        USING(project_id IS NULL OR hub.can_read_scope(project_id,repository_id));

      -- Legacy source IDs equal project IDs, so the old HTTP routes remain stable.
      ALTER TABLE document_repositories ADD COLUMN id uuid;
      UPDATE document_repositories SET id=project_id;
      ALTER TABLE document_repositories ALTER COLUMN id SET NOT NULL;
      ALTER TABLE document_repositories ADD COLUMN repository_id uuid;
      ALTER TABLE document_repositories ADD CONSTRAINT document_repository_project
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id);
      ALTER TABLE document_repositories DROP CONSTRAINT document_repositories_pkey;
      ALTER TABLE document_repositories ADD PRIMARY KEY(id);
      ALTER TABLE document_repositories ADD UNIQUE(id,project_id);
      ALTER TABLE document_repositories ADD CHECK(id=coalesce(repository_id,project_id));
      CREATE INDEX ix_document_repositories_project_id ON document_repositories(project_id);
      ALTER TABLE document_syncs ADD COLUMN source_id uuid;
      UPDATE document_syncs SET source_id=project_id;
      ALTER TABLE document_syncs ALTER COLUMN source_id SET NOT NULL;
      ALTER TABLE document_syncs ADD FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id);
      DROP INDEX one_document_sync_active;
      CREATE UNIQUE INDEX one_document_sync_active ON document_syncs(source_id)
        WHERE status IN ('queued','running');
      ALTER TABLE project_documents ADD COLUMN source_id uuid;
      UPDATE project_documents SET source_id=project_id;
      ALTER TABLE project_documents ALTER COLUMN source_id SET NOT NULL;
      ALTER TABLE project_documents DROP CONSTRAINT project_documents_pkey;
      ALTER TABLE project_documents ADD PRIMARY KEY(source_id,path);
      ALTER TABLE project_documents ADD FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id);
      CREATE POLICY source_scope ON document_repositories AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,id)) WITH CHECK(hub.can_read_source(project_id,id));
      CREATE POLICY document_scope ON project_documents AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,source_id)) WITH CHECK(hub.can_read_source(project_id,source_id));
      CREATE POLICY sync_scope ON document_syncs AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,source_id)) WITH CHECK(hub.can_read_source(project_id,source_id));

      CREATE TABLE repository_tickets (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        session_id uuid NOT NULL REFERENCES auth_sessions(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), digest varchar(64) NOT NULL UNIQUE,
        action varchar(16) NOT NULL CHECK(action IN ('read','write')),
        run_id uuid, lease_epoch integer, expected_commit varchar(64), expires_at timestamptz NOT NULL,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_repository_tickets_expires_at ON repository_tickets(expires_at);
      GRANT SELECT,INSERT,DELETE ON repository_tickets TO hub_runtime;
      ALTER TABLE repository_tickets ENABLE ROW LEVEL SECURITY;
      ALTER TABLE repository_tickets FORCE ROW LEVEL SECURITY;
      CREATE POLICY ticket_owner ON repository_tickets TO hub_owner USING(true) WITH CHECK(true);
      -- Lookup only discloses an unguessable digest to the API; authorization is checked on every Git leg.
      CREATE POLICY ticket_lookup ON repository_tickets FOR SELECT TO hub_runtime USING(true);
      CREATE POLICY ticket_insert ON repository_tickets FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND
        device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND
        hub.has_repository_cap(project_id,repository_id,CASE WHEN action='read' THEN 'code:read' ELSE 'code:write' END));
      CREATE POLICY ticket_expiry ON repository_tickets FOR DELETE TO hub_runtime USING(expires_at<now());
    """)
    # Existing policies still govern roles, ownership and logs:read. Resource visibility
    # is an additional restriction, including direct SQL and summary/context reads.
    for table in ("task_runs", "worklogs", "task_decisions", "work_contexts"):
        op.execute(f"""
          CREATE POLICY inherited_repository_scope ON {table} AS RESTRICTIVE TO hub_runtime
            USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id={table}.task_id AND t.project_id={table}.project_id))
            WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id={table}.task_id AND t.project_id={table}.project_id));
        """)


def downgrade():
    raise RuntimeError("Preserve grants and collaboration history; roll back application images without dropping data")
