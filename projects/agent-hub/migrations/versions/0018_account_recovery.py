"""Proof-bound account claims and credential recovery; no implicit device grants."""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE account_invitations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), project_id uuid REFERENCES projects(id),
        created_by uuid NOT NULL REFERENCES users(id), email varchar(254) NOT NULL,
        expires_at timestamptz NOT NULL, consumed_at timestamptz
      );
      CREATE INDEX ix_account_invitations_user_id ON account_invitations(user_id);
      CREATE TABLE account_challenges (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), invitation_id uuid REFERENCES account_invitations(id),
        email varchar(254) NOT NULL, purpose varchar(16) NOT NULL CHECK(purpose IN ('reset','invitation')),
        thumbprint varchar(64) NOT NULL, user_epoch integer NOT NULL CHECK(user_epoch>0),
        digest varchar(64) NOT NULL, expires_at timestamptz NOT NULL,
        attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5), consumed_at timestamptz,
        CHECK((purpose='invitation')=(invitation_id IS NOT NULL))
      );
      CREATE INDEX ix_account_challenges_user_id ON account_challenges(user_id);
      GRANT SELECT,INSERT ON account_invitations,account_challenges TO hub_runtime;
      GRANT UPDATE(consumed_at) ON account_invitations TO hub_runtime;
      GRANT UPDATE(consumed_at,attempts) ON account_challenges TO hub_runtime;
      CREATE POLICY account_recovery_project_read ON projects FOR SELECT TO hub_owner USING(true);
      CREATE FUNCTION hub.account_invitation_eligible(iid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.account_invitations i JOIN public.users u ON u.id=i.user_id
          WHERE i.id=iid AND i.consumed_at IS NULL AND i.expires_at>now()
          AND u.status='pending_email' AND u.email=i.email AND (i.project_id IS NULL OR EXISTS(
            SELECT 1 FROM public.projects p JOIN public.project_memberships m ON m.project_id=p.id
            WHERE p.id=i.project_id AND p.status='active' AND m.user_id=u.id AND m.status='active')))
      $$;
      REVOKE ALL ON FUNCTION hub.account_invitation_eligible(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.account_invitation_eligible(uuid) TO hub_runtime;
      CREATE FUNCTION hub.account_closeout_allowed(uid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.users u JOIN public.devices d ON d.user_id=u.id
          WHERE u.id=uid AND u.id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND d.id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND u.status='active' AND d.status='approved'
          AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL)))
        OR EXISTS(SELECT 1 FROM public.account_challenges c JOIN public.users u ON u.id=c.user_id
          WHERE u.id=uid AND c.id=nullif(current_setting('hub.account_recovery_id',true),'')::uuid
          AND c.digest=current_setting('hub.account_recovery_digest',true)
          AND c.thumbprint=current_setting('hub.account_recovery_thumb',true)
          AND c.consumed_at IS NULL AND c.expires_at>now() AND c.attempts<=5
          AND u.auth_epoch=c.user_epoch AND u.email=c.email AND u.status IN ('active','pending_email'))
      $$;
      CREATE FUNCTION hub.account_closeout_scopes(uid uuid)
      RETURNS TABLE(project_id uuid,user_id uuid,device_id uuid)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT g.project_id,g.user_id,g.device_id FROM public.access_grants g
          WHERE g.user_id=uid AND hub.account_closeout_allowed(uid)
        UNION SELECT r.project_id,r.user_id,r.device_id FROM public.task_runs r
          WHERE r.user_id=uid AND hub.account_closeout_allowed(uid)
      $$;
      REVOKE ALL ON FUNCTION hub.account_closeout_allowed(uuid),hub.account_closeout_scopes(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.account_closeout_allowed(uuid),hub.account_closeout_scopes(uuid)
        TO hub_runtime;
      CREATE POLICY closeout_account_create ON closeout_jobs FOR INSERT TO hub_runtime WITH CHECK(
        hub.account_closeout_allowed(user_id) AND reason IN ('credential_recovery','device_revoked')
        AND EXISTS(SELECT 1 FROM devices d WHERE d.id=device_id AND d.user_id=closeout_jobs.user_id));
      CREATE POLICY closeout_account_return ON closeout_jobs FOR SELECT TO hub_runtime USING(
        hub.account_closeout_allowed(user_id) AND reason IN ('credential_recovery','device_revoked'));
      DO $migration$
      DECLARE definition text; needle text := 'NOT hub.has_project_cap(c.project_id,''members:manage'')';
      BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,needle,''))<>length(needle) THEN
          RAISE EXCEPTION 'Unexpected closeout authorization definition';
        END IF;
        definition := replace(definition,needle,
          'NOT (hub.has_project_cap(c.project_id,''members:manage'') OR
            (hub.account_closeout_allowed(c.user_id)
              AND c.reason IN (''credential_recovery'',''device_revoked'')))');
        EXECUTE definition;
      END $migration$;
    """)


def downgrade():
    raise RuntimeError("Keep credential recovery history; restore a verified backup instead")
