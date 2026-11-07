"""Runtime privileges and project isolation.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("CREATE SCHEMA hub")
    op.execute("REVOKE ALL ON SCHEMA hub FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public, hub TO hub_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hub_runtime")
    op.execute("REVOKE ALL ON alembic_version FROM hub_runtime")
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM hub_runtime")
    op.execute("""
        CREATE FUNCTION hub.is_superadmin() RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.users u JOIN public.devices d ON d.user_id = u.id
            WHERE u.id = nullif(current_setting('hub.user_id', true), '')::uuid
              AND d.id = nullif(current_setting('hub.device_id', true), '')::uuid
              AND u.platform_role = 'superadmin' AND u.status = 'active'
              AND u.email_verified_at IS NOT NULL AND d.status = 'approved'
              AND d.email_verified_at IS NOT NULL
          )
        $$
    """)
    op.execute("""
        CREATE FUNCTION hub.can_access_project(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.users u
            JOIN public.devices d ON d.user_id = u.id
            JOIN public.project_memberships m ON m.user_id = u.id
            JOIN public.access_grants g ON g.project_id = m.project_id AND g.user_id = u.id
            WHERE u.id = nullif(current_setting('hub.user_id', true), '')::uuid
              AND d.id = nullif(current_setting('hub.device_id', true), '')::uuid
              AND g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.device_id = d.id AND m.project_id = pid
              AND u.status = 'active' AND d.status = 'approved' AND m.status = 'active'
              AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL))
              AND g.status = 'active' AND g.expires_at > now()
              AND g.membership_epoch = m.epoch AND g.user_epoch = u.auth_epoch AND g.device_epoch = d.epoch
              AND g.capabilities ? 'project:read'
          )
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) TO hub_runtime")
    op.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE projects FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY project_read ON projects FOR SELECT "
        "USING (hub.is_superadmin() OR (status='active' AND hub.can_access_project(id)))"
    )
    op.execute("CREATE POLICY project_insert ON projects FOR INSERT WITH CHECK (hub.is_superadmin())")
    op.execute(
        "CREATE POLICY project_update ON projects FOR UPDATE "
        "USING (hub.is_superadmin()) WITH CHECK (hub.is_superadmin())"
    )
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY audit_insert ON audit_events FOR INSERT WITH CHECK (true)")
    op.execute("""
        CREATE POLICY audit_read ON audit_events FOR SELECT USING (
          hub.is_superadmin() OR (
            project_id IS NOT NULL AND hub.can_access_project(project_id)
            AND EXISTS (SELECT 1 FROM public.access_grants g
                        WHERE g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
                          AND g.project_id = audit_events.project_id AND g.capabilities ? 'audit:read')
          )
        )
    """)


def downgrade():
    op.execute("DROP POLICY audit_read ON audit_events")
    op.execute("DROP POLICY audit_insert ON audit_events")
    op.execute("ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY project_update ON projects")
    op.execute("DROP POLICY project_insert ON projects")
    op.execute("DROP POLICY project_read ON projects")
    op.execute("ALTER TABLE projects DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE projects NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP SCHEMA hub CASCADE")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM hub_runtime")
