"""Immutable usage observations, explicit budgets and source-bound retrieval evaluations."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE usage_policies (
        project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
      );
      CREATE TABLE usage_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid, task_id uuid NOT NULL, run_id uuid NOT NULL,
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        reference_hash varchar(64) NOT NULL, source varchar(24) NOT NULL CHECK(source IN ('host-report','context-measurement')),
        provider varchar(100) NOT NULL, model varchar(200) NOT NULL,
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        cached_input_tokens bigint CHECK(cached_input_tokens BETWEEN 0 AND input_tokens),
        reported_cost_micro_usd bigint CHECK(reported_cost_micro_usd BETWEEN 0 AND 1000000000),
        context_bytes bigint CHECK(context_bytes BETWEEN 0 AND 1048576),
        UNIQUE(project_id,run_id,source,reference_hash),
        FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK((source='host-report' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
          AND context_bytes IS NULL) OR (source='context-measurement' AND context_bytes IS NOT NULL
          AND input_tokens IS NULL AND output_tokens IS NULL AND reported_cost_micro_usd IS NULL))
      );
      CREATE INDEX ix_usage_project_period ON usage_events(project_id,created_at);
      CREATE TABLE memory_evaluations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        payload jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_memory_evaluations_project ON memory_evaluations(project_id,created_at,id);
      GRANT SELECT,INSERT ON usage_policies,usage_events,memory_evaluations TO hub_runtime;
    """)
    for table in ("usage_policies", "usage_events", "memory_evaluations"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY usage_policy_read ON usage_policies FOR SELECT TO hub_runtime USING(hub.can_access_project(project_id));
      CREATE POLICY usage_policy_write ON usage_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY usage_read ON usage_events FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (hub.has_project_cap(project_id,'tasks:publish')
        OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
      CREATE POLICY usage_insert ON usage_events FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write') AND hub.can_read_scope(project_id,repository_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=run_id
          AND r.user_id=usage_events.author_id AND r.device_id=usage_events.device_id
          AND t.repository_id IS NOT DISTINCT FROM usage_events.repository_id));
      CREATE POLICY evaluation_read ON memory_evaluations FOR SELECT TO hub_runtime USING(
        hub.has_project_cap(project_id,'tasks:publish') AND hub.can_access_project(project_id)
        AND (repository_id IS NULL OR hub.can_read_scope(project_id,repository_id))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(payload->'source_card_ids') s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=s::uuid AND c.project_id=memory_evaluations.project_id)));
      CREATE POLICY evaluation_insert ON memory_evaluations FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish') AND hub.can_access_project(project_id)
        AND (repository_id IS NULL OR hub.can_read_scope(project_id,repository_id))
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(payload->'source_card_ids') s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=s::uuid AND c.project_id=memory_evaluations.project_id)));
      CREATE FUNCTION hub.usage_budget_open(pid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND COALESCE((
          SELECT (p.content->>'mode')<>'pause-managed-work'
            OR (p.content->>'monthly_reported_token_limit')::bigint=0
            OR (SELECT COALESCE(sum(e.input_tokens+e.output_tokens),0) FROM public.usage_events e
                WHERE e.project_id=pid AND e.source='host-report'
                AND e.created_at>=date_trunc('month',CURRENT_TIMESTAMP AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
              <(p.content->>'monthly_reported_token_limit')::bigint
          FROM public.usage_policies p WHERE p.project_id=pid ORDER BY p.version DESC LIMIT 1),true)
      $$;
      REVOKE ALL ON FUNCTION hub.usage_budget_open(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.usage_budget_open(uuid) TO hub_runtime;
    """)


def downgrade():
    raise RuntimeError("Preserve usage and evaluation evidence; restore a verified backup")
