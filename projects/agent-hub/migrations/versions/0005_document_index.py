"""Project-scoped Git document snapshots and a separately privileged index worker."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Provision the NOLOGIN hub_indexer role before migration; API is never a member.
    op.execute("""
        CREATE FUNCTION hub.can_read_documents(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? 'documents:read'
          ))
        $$;
        CREATE FUNCTION hub.can_manage_documents(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g JOIN public.project_memberships m
              ON m.project_id=g.project_id AND m.user_id=g.user_id
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? 'documents:manage'
              AND m.role='administrator' AND m.status='active'
          ))
        $$;
        REVOKE ALL ON FUNCTION hub.can_read_documents(uuid), hub.can_manage_documents(uuid) FROM PUBLIC;
        GRANT USAGE ON SCHEMA public, hub TO hub_indexer;
        GRANT EXECUTE ON FUNCTION hub.can_read_documents(uuid), hub.can_manage_documents(uuid)
          TO hub_runtime, hub_indexer;
        GRANT EXECUTE ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) TO hub_indexer;
        CREATE TABLE document_repositories (
          project_id uuid PRIMARY KEY REFERENCES projects(id), repository_url varchar(400) NOT NULL,
          branch varchar(200) NOT NULL, include_paths jsonb NOT NULL, encrypted_token text,
          version integer NOT NULL CHECK (version>0), snapshot_id uuid, git_commit varchar(64),
          indexed_at timestamptz, document_count integer NOT NULL DEFAULT 0,
          skipped_count integer NOT NULL DEFAULT 0
        );
        CREATE TABLE document_syncs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), configuration_version integer NOT NULL,
          requested_by uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          grant_id uuid REFERENCES access_grants(id), status varchar(20) NOT NULL DEFAULT 'queued',
          attempts integer NOT NULL DEFAULT 0, lease_until timestamptz, lease_id uuid,
          finished_at timestamptz, error_code varchar(80),
          CHECK (status IN ('queued','running','succeeded','failed','cancelled'))
        );
        CREATE INDEX ix_document_syncs_project ON document_syncs(project_id, created_at DESC);
        CREATE UNIQUE INDEX one_document_sync_active ON document_syncs(project_id)
          WHERE status IN ('queued','running');
        CREATE TABLE project_documents (
          project_id uuid NOT NULL REFERENCES projects(id), path varchar(800) NOT NULL,
          snapshot_id uuid NOT NULL, title varchar(300) NOT NULL, content text NOT NULL,
          sha256 varchar(64) NOT NULL, byte_size integer NOT NULL, PRIMARY KEY(project_id, path)
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON document_repositories, document_syncs TO hub_runtime;
        GRANT SELECT, DELETE ON project_documents TO hub_runtime;
        GRANT SELECT, UPDATE ON document_repositories, document_syncs TO hub_indexer;
        GRANT SELECT, INSERT, DELETE ON project_documents TO hub_indexer;
        GRANT SELECT ON projects, users, devices, project_memberships, access_grants TO hub_indexer;
        GRANT INSERT ON audit_events TO hub_indexer;
        CREATE POLICY project_indexer ON projects FOR SELECT TO hub_indexer USING (true);
    """)
    for table in ("document_repositories", "document_syncs", "project_documents"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY indexer ON {table} TO hub_indexer USING (true) WITH CHECK (true)")
    op.execute("""
        CREATE POLICY repository_read ON document_repositories FOR SELECT TO hub_runtime
          USING (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id));
        CREATE POLICY repository_manage ON document_repositories FOR ALL TO hub_runtime
          USING (hub.can_manage_documents(project_id)) WITH CHECK (hub.can_manage_documents(project_id));
        CREATE POLICY sync_manage ON document_syncs FOR ALL TO hub_runtime
          USING (hub.can_manage_documents(project_id)) WITH CHECK (hub.can_manage_documents(project_id));
        CREATE POLICY document_read ON project_documents FOR SELECT TO hub_runtime
          USING (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id));
        CREATE POLICY document_clear ON project_documents FOR DELETE TO hub_runtime
          USING (hub.can_manage_documents(project_id));
    """)


def downgrade():
    op.execute("DROP POLICY project_indexer ON projects")
    for table in ("project_documents", "document_syncs", "document_repositories"):
        op.drop_table(table)
    op.execute("DROP FUNCTION hub.can_manage_documents(uuid), hub.can_read_documents(uuid)")
