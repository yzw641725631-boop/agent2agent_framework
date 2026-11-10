"""Hashed, expiring one-use account recovery; device and grant admission is unchanged."""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE account_recovery_codes (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        batch_id uuid NOT NULL, user_epoch integer NOT NULL CHECK(user_epoch>0),
        digest varchar(64) NOT NULL UNIQUE CHECK(digest~'^[a-f0-9]{64}$'),
        expires_at timestamptz NOT NULL, consumed_at timestamptz
      );
      CREATE INDEX ix_recovery_codes_user ON account_recovery_codes(user_id,batch_id);
      ALTER TABLE account_recovery_codes ENABLE ROW LEVEL SECURITY;
      ALTER TABLE account_recovery_codes FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_access ON account_recovery_codes TO hub_owner USING(true) WITH CHECK(true);
      GRANT SELECT,INSERT ON account_recovery_codes TO hub_runtime;
      GRANT UPDATE(consumed_at) ON account_recovery_codes TO hub_runtime;
      CREATE POLICY recovery_own_read ON account_recovery_codes FOR SELECT TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid);
      CREATE POLICY recovery_own_insert ON account_recovery_codes FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.account_closeout_allowed(user_id));
      CREATE POLICY recovery_own_retire ON account_recovery_codes FOR UPDATE TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid)
        WITH CHECK(user_id=nullif(current_setting('hub.user_id',true),'')::uuid);
      CREATE FUNCTION hub.offline_recovery_user(code_hash text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT c.user_id FROM public.account_recovery_codes c JOIN public.users u ON u.id=c.user_id
          WHERE c.digest=code_hash AND c.consumed_at IS NULL AND c.expires_at>now()
          AND c.user_epoch=u.auth_epoch AND u.status='active'
      $$;
      REVOKE ALL ON FUNCTION hub.offline_recovery_user(text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.offline_recovery_user(text) TO hub_runtime;
      DO $migration$
      DECLARE definition text; marker text := 'SELECT EXISTS(';
      BEGIN
        SELECT pg_get_functiondef('hub.account_closeout_allowed(uuid)'::regprocedure) INTO definition;
        IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'Unknown recovery authorization'; END IF;
        definition=replace(definition,marker,
          'SELECT uid=hub.offline_recovery_user(current_setting(''hub.offline_recovery_digest'',true)) OR EXISTS(');
        EXECUTE definition;
      END $migration$;
      CREATE FUNCTION hub.consume_offline_recovery(code_hash text) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE cid uuid;
      BEGIN
        IF code_hash IS DISTINCT FROM current_setting('hub.offline_recovery_digest',true)
          OR hub.offline_recovery_user(code_hash) IS NULL THEN RETURN false; END IF;
        UPDATE public.account_recovery_codes SET consumed_at=now() WHERE digest=code_hash
          AND consumed_at IS NULL AND expires_at>now() RETURNING id INTO cid;
        RETURN cid IS NOT NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.consume_offline_recovery(text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.consume_offline_recovery(text) TO hub_runtime;
    """)


def downgrade():
    raise RuntimeError("Keep recovery consumption history; use a verified application rollback")
