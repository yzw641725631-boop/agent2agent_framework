"""Dedicated server generation role and append-only source-bound provenance."""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE generation_configuration (
        key varchar(32) PRIMARY KEY CHECK(key='provider'), version integer NOT NULL CHECK(version>0),
        encrypted_value text NOT NULL
      );
      CREATE TABLE generation_policies (
        repository_id uuid NOT NULL, project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        instance_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), content jsonb NOT NULL,
        PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE generation_calls (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        policy_version integer NOT NULL, provider_version integer NOT NULL, provider_model varchar(120) NOT NULL,
        kind varchar(20) NOT NULL CHECK(kind IN ('distillation','guide')), job_id uuid NOT NULL,
        state varchar(20) NOT NULL CHECK(state IN ('reserved','calling','ready','shared','uncertain','blocked','abandoned')),
        lease_token uuid NOT NULL, lease_until timestamptz NOT NULL,
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        job_version integer NOT NULL, job_lease_epoch integer NOT NULL, source_hash varchar(64) NOT NULL,
        reserved_tokens integer NOT NULL CHECK(reserved_tokens>0),
        input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 65536),
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        response_hash varchar(64), encrypted_result text, error_code varchar(100), finished_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE UNIQUE INDEX uq_generation_job_once ON generation_calls(kind,job_id) WHERE state<>'abandoned';
      CREATE INDEX ix_generation_daily ON generation_calls(created_at);
      CREATE TABLE generation_retry_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        call_id uuid NOT NULL REFERENCES generation_calls(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), allow_new_provider_call boolean NOT NULL,
        reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10),
        state varchar(20) NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','rejected')),
        error_code varchar(100)
      );
      CREATE UNIQUE INDEX uq_generation_retry_pending ON generation_retry_requests(call_id) WHERE state='pending';
      GRANT SELECT,INSERT ON generation_retry_requests TO hub_runtime;
      GRANT UPDATE ON generation_retry_requests TO hub_generation;
      GRANT SELECT,INSERT,UPDATE ON generation_configuration TO hub_runtime;
      GRANT SELECT,INSERT ON generation_policies TO hub_runtime;
      GRANT SELECT ON generation_calls TO hub_runtime;
      GRANT SELECT ON generation_configuration,generation_policies TO hub_generation;
      GRANT INSERT,UPDATE ON generation_calls TO hub_generation;
      CREATE TABLE generation_receipts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        distillation_id uuid REFERENCES distillation_jobs(id), guide_id uuid REFERENCES guide_jobs(id),
        lease_epoch integer NOT NULL CHECK(lease_epoch>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), provider varchar(32) NOT NULL CHECK(provider='openai-compatible'),
        model varchar(120) NOT NULL, response_hash varchar(64) NOT NULL CHECK(response_hash~'^[a-f0-9]{64}$'),
        input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 65536),
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        CHECK(num_nonnulls(distillation_id,guide_id)=1),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        UNIQUE(distillation_id,lease_epoch), UNIQUE(guide_id,lease_epoch)
      );
      ALTER TABLE generation_receipts ENABLE ROW LEVEL SECURITY;
      ALTER TABLE generation_receipts FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_access ON generation_receipts TO hub_owner USING(true) WITH CHECK(true);
      GRANT SELECT ON generation_receipts TO hub_runtime;
      GRANT INSERT ON generation_receipts TO hub_generation;
      CREATE POLICY receipt_read ON generation_receipts FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=distillation_id)
          OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=guide_id)));
      CREATE POLICY receipt_insert ON generation_receipts FOR INSERT TO hub_generation WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.has_project_cap(project_id,'tasks:write') AND hub.can_read_scope(project_id,repository_id)
        AND (distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j
          WHERE j.id=distillation_id AND j.project_id=generation_receipts.project_id
            AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
            AND j.target_user_id=author_id AND j.target_device_id=device_id
            AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='succeeded')
        OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j
          WHERE j.id=guide_id AND j.project_id=generation_receipts.project_id
            AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
            AND j.target_user_id=author_id AND j.target_device_id=device_id
            AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='review')));
    """)
    for table in (
        "generation_configuration",
        "generation_policies",
        "generation_calls",
        "generation_retry_requests",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY configuration_root ON generation_configuration TO hub_runtime
        USING(hub.is_superadmin()) WITH CHECK(hub.is_superadmin());
      CREATE POLICY configuration_worker ON generation_configuration FOR SELECT TO hub_generation USING(true);
      CREATE POLICY generation_policy_read ON generation_policies FOR SELECT TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY generation_policy_insert ON generation_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND hub.can_read_scope(project_id,repository_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY generation_policy_worker ON generation_policies FOR SELECT TO hub_generation USING(true);
      CREATE POLICY generation_calls_read ON generation_calls FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:publish'));
      CREATE POLICY generation_calls_worker ON generation_calls TO hub_generation USING(true) WITH CHECK(true);
      CREATE POLICY generation_retry_read ON generation_retry_requests FOR SELECT TO hub_runtime USING(
        hub.is_superadmin());
      CREATE POLICY generation_retry_insert ON generation_retry_requests FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND state='pending' AND error_code IS NULL
        AND user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM generation_calls c WHERE c.id=call_id AND c.state IN ('blocked','uncertain')));
      CREATE POLICY generation_retry_worker ON generation_retry_requests TO hub_generation USING(true) WITH CHECK(true);
      CREATE FUNCTION hub.generation_project_budget_open(pid uuid, additional bigint) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND additional>=0 AND COALESCE((
          SELECT (p.content->>'mode')<>'pause-managed-work' OR (p.content->>'monthly_reported_token_limit')::bigint=0
            OR (SELECT COALESCE(sum(e.input_tokens+e.output_tokens),0) FROM usage_events e
                WHERE e.project_id=pid AND e.source='host-report' AND e.created_at>=date_trunc('month',now()))
             +(SELECT COALESCE(sum(c.reserved_tokens),0) FROM generation_calls c
                WHERE c.project_id=pid AND c.created_at>=date_trunc('month',now()))+additional
              <=(p.content->>'monthly_reported_token_limit')::bigint
          FROM usage_policies p WHERE p.project_id=pid ORDER BY p.version DESC LIMIT 1),true)
        $$;
      REVOKE ALL ON FUNCTION hub.generation_project_budget_open(uuid,bigint) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.generation_project_budget_open(uuid,bigint) TO hub_runtime;
    """)


def downgrade():
    raise RuntimeError("Generation provenance is retained; use a verified application rollback")
