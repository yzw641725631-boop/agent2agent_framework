"""Preapproved OAuth clients, PKCE authorization codes and rotating resource-bound sessions."""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE oauth_clients (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id varchar(100) NOT NULL UNIQUE, name varchar(200) NOT NULL,
        author_id uuid NOT NULL REFERENCES users(id), approver_device_id uuid NOT NULL REFERENCES devices(id),
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        grant_id uuid REFERENCES access_grants(id), project_id uuid NOT NULL REFERENCES projects(id),
        repository_id uuid NOT NULL REFERENCES project_repositories(id), redirect_uris jsonb NOT NULL,
        scopes jsonb NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        status varchar(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
        CHECK(jsonb_typeof(redirect_uris)='array' AND jsonb_array_length(redirect_uris) BETWEEN 1 AND 5),
        CHECK(jsonb_typeof(scopes)='array' AND scopes <@ '["mcp:read","mcp:coordinate"]'::jsonb),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE oauth_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id uuid NOT NULL REFERENCES oauth_clients(id), client_version integer NOT NULL,
        redirect_uri varchar(2000) NOT NULL, resource varchar(1000) NOT NULL, scopes jsonb NOT NULL,
        state varchar(2048) NOT NULL, challenge varchar(43) NOT NULL, expires_at timestamptz NOT NULL,
        session_id uuid REFERENCES auth_sessions(id), code_digest varchar(64) UNIQUE,
        decided_at timestamptz, consumed_at timestamptz
      );
      CREATE TABLE oauth_sessions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id uuid NOT NULL REFERENCES oauth_clients(id), client_version integer NOT NULL,
        session_id uuid NOT NULL REFERENCES auth_sessions(id), resource varchar(1000) NOT NULL,
        scopes jsonb NOT NULL, family_id uuid NOT NULL, access_digest varchar(64) NOT NULL UNIQUE,
        refresh_digest varchar(64) NOT NULL UNIQUE, access_expires_at timestamptz NOT NULL,
        refresh_expires_at timestamptz NOT NULL,
        state varchar(16) NOT NULL DEFAULT 'active' CHECK(state IN ('active','rotated','revoked'))
      );
      CREATE INDEX ix_oauth_session_family ON oauth_sessions(family_id);
      CREATE INDEX ix_oauth_request_expiry ON oauth_requests(expires_at);
      GRANT SELECT,INSERT ON oauth_clients TO hub_runtime;
      GRANT UPDATE(status,version) ON oauth_clients TO hub_runtime;
      GRANT SELECT,INSERT ON oauth_requests,oauth_sessions TO hub_runtime;
      GRANT UPDATE(session_id,code_digest,decided_at,consumed_at) ON oauth_requests TO hub_runtime;
      GRANT UPDATE(state) ON oauth_sessions TO hub_runtime;
    """)
    for table in ("oauth_clients", "oauth_requests", "oauth_sessions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY oauth_client_read ON oauth_clients FOR SELECT TO hub_runtime USING(true);
      CREATE POLICY oauth_client_insert ON oauth_clients FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND approver_device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY oauth_client_revoke ON oauth_clients FOR UPDATE TO hub_runtime
        USING(hub.is_superadmin()) WITH CHECK(hub.is_superadmin());
      -- Protocol storage contains hashes and fixed approval references. Public code
      -- exchange needs lookup before an authenticated context exists, like auth_sessions.
      CREATE POLICY oauth_protocol ON oauth_requests TO hub_runtime USING(true) WITH CHECK(true);
      CREATE POLICY oauth_protocol ON oauth_sessions TO hub_runtime USING(true) WITH CHECK(true);
    """)


def downgrade():
    raise RuntimeError("Retain OAuth approval and rotation history; use a verified application rollback")
