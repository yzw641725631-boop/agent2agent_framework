"""Repository-scoped registered recipients, durable task offers and turn references."""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE worklogs ADD COLUMN turn_id uuid;
      CREATE INDEX ix_worklogs_run_turn ON worklogs(run_id,turn_id) WHERE turn_id IS NOT NULL;
      CREATE TABLE agent_endpoints (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        grant_id uuid REFERENCES access_grants(id), user_epoch integer NOT NULL, device_epoch integer NOT NULL,
        instance_id uuid NOT NULL, name varchar(100) NOT NULL, harness varchar(24) NOT NULL,
        roles jsonb NOT NULL CHECK(jsonb_typeof(roles)='array' AND jsonb_array_length(roles) BETWEEN 1 AND 4
          AND roles <@ '["project-manager","architect","designer","developer"]'::jsonb),
        enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        last_seen_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(id,project_id), UNIQUE(project_id,device_id,instance_id),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE agent_dispatches (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, endpoint_id uuid NOT NULL,
        sender_id uuid NOT NULL REFERENCES users(id), sender_device_id uuid NOT NULL REFERENCES devices(id),
        task_version integer NOT NULL CHECK(task_version>0), endpoint_version integer NOT NULL CHECK(endpoint_version>0),
        note varchar(1000) NOT NULL, state varchar(16) NOT NULL DEFAULT 'queued'
          CHECK(state IN ('queued','received','accepted','declined','cancelled','expired')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), expires_at timestamptz NOT NULL,
        received_at timestamptz, decided_at timestamptz, reason varchar(1000) NOT NULL DEFAULT '', run_id uuid,
        FOREIGN KEY(endpoint_id,project_id) REFERENCES agent_endpoints(id,project_id),
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
        CHECK((state='accepted')=(run_id IS NOT NULL))
      );
      CREATE UNIQUE INDEX uq_agent_dispatch_pending_task ON agent_dispatches(task_id) WHERE state IN ('queued','received');
      CREATE INDEX ix_agent_dispatch_inbox ON agent_dispatches(endpoint_id,created_at,id);
      CREATE INDEX ix_agent_endpoint_project ON agent_endpoints(project_id,repository_id);
      CREATE FUNCTION hub.guard_agent_dispatch_transition() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        IF OLD.state NOT IN ('queued','received') OR NEW.version<>OLD.version+1
          OR NEW.state NOT IN ('received','accepted','declined','cancelled','expired')
          OR (OLD.state='received' AND NEW.state='received')
          OR (NEW.state IN ('received','accepted','declined') AND NEW.expires_at<=now())
          OR (NEW.state='expired' AND NEW.expires_at>now())
          OR (NEW.state IN ('received','accepted') AND NEW.received_at IS NULL)
          OR (NEW.state<>'received' AND NEW.decided_at IS NULL)
          OR (NEW.state IN ('declined','cancelled') AND length(trim(NEW.reason))=0)
        THEN RAISE EXCEPTION 'invalid dispatch transition' USING ERRCODE='23514'; END IF;
        IF NEW.state='accepted' AND NOT EXISTS(
          SELECT 1 FROM public.task_runs r JOIN public.tasks t ON t.id=r.task_id
          JOIN public.agent_endpoints a ON a.id=NEW.endpoint_id
          WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.project_id=NEW.project_id
            AND r.user_id=a.user_id AND r.device_id=a.device_id AND r.instance_id=a.instance_id AND r.harness=a.harness
            AND r.state='active' AND t.active_run_id=r.id AND t.status='in_progress'
            AND t.lease_until>now())
        THEN RAISE EXCEPTION 'dispatch requires matching active claim' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_agent_dispatch_transition() FROM PUBLIC;
      CREATE TRIGGER agent_dispatch_transition BEFORE UPDATE OF state,version,received_at,decided_at,reason,run_id ON agent_dispatches
        FOR EACH ROW EXECUTE FUNCTION hub.guard_agent_dispatch_transition();
      GRANT SELECT,INSERT ON agent_endpoints,agent_dispatches TO hub_runtime;
      GRANT UPDATE(grant_id,user_epoch,device_epoch,name,roles,enabled,version,last_seen_at) ON agent_endpoints TO hub_runtime;
      GRANT UPDATE(state,version,received_at,decided_at,reason,run_id) ON agent_dispatches TO hub_runtime;
    """)
    for table in ("agent_endpoints", "agent_dispatches"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY agent_owner ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE FUNCTION hub.agent_endpoint_eligible(aid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.agent_endpoints a
          JOIN public.users u ON u.id=a.user_id JOIN public.devices d ON d.id=a.device_id AND d.user_id=u.id
          JOIN public.projects p ON p.id=a.project_id
          WHERE a.id=aid AND a.enabled AND p.status='active'
          AND u.status='active' AND d.status='approved' AND a.user_epoch=u.auth_epoch AND a.device_epoch=d.epoch
          AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL))
          AND hub.can_read_scope(a.project_id,a.repository_id)
          AND ((a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
                AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid)
               OR hub.has_project_cap(a.project_id,'tasks:publish'))
          AND (a.repository_id IS NULL OR EXISTS(SELECT 1 FROM public.project_repositories r
            WHERE r.id=a.repository_id AND r.project_id=a.project_id AND r.status='active'))
          AND ((u.platform_role='superadmin' AND u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL)
            OR EXISTS(SELECT 1 FROM public.project_memberships m JOIN public.access_grants g
              ON g.project_id=m.project_id AND g.user_id=m.user_id
              WHERE m.project_id=a.project_id AND m.user_id=a.user_id AND m.status='active'
              AND g.id=a.grant_id AND g.device_id=a.device_id AND g.status='active' AND g.expires_at>now()
              AND g.membership_epoch=m.epoch AND g.user_epoch=u.auth_epoch AND g.device_epoch=d.epoch
              AND g.capabilities ?& array['project:read','tasks:claim','tasks:write']
              AND (a.repository_id IS NULL AND p.repository_mode='legacy' OR g.repository_permissions @>
                jsonb_build_array(jsonb_build_object('repository_id',a.repository_id::text,'capabilities',jsonb_build_array('code:read','code:write')))))))
      $$;
      REVOKE ALL ON FUNCTION hub.agent_endpoint_eligible(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.agent_endpoint_eligible(uuid) TO hub_runtime;
      CREATE POLICY agent_endpoint_read ON agent_endpoints FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          OR hub.has_project_cap(project_id,'tasks:publish') AND hub.agent_endpoint_eligible(id)));
      CREATE POLICY agent_endpoint_insert ON agent_endpoints FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.has_project_cap(project_id,'tasks:claim') AND hub.has_project_cap(project_id,'tasks:write')
        AND hub.can_read_scope(project_id,repository_id) AND (repository_id IS NULL OR hub.has_repository_cap(project_id,repository_id,'code:write')));
      CREATE POLICY agent_endpoint_update ON agent_endpoints FOR UPDATE TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:write'))
        WITH CHECK(user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:write'));
      CREATE POLICY agent_dispatch_read ON agent_dispatches FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') OR EXISTS(SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id
          AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND hub.agent_endpoint_eligible(a.id))));
      CREATE POLICY agent_dispatch_insert ON agent_dispatches FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish') AND sender_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND sender_device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND state='queued' AND version=1
        AND hub.agent_endpoint_eligible(endpoint_id) AND EXISTS(SELECT 1 FROM tasks t JOIN agent_endpoints a
          ON a.project_id=t.project_id AND a.repository_id IS NOT DISTINCT FROM t.repository_id
          WHERE t.id=task_id AND a.id=endpoint_id AND t.project_id=agent_dispatches.project_id));
      CREATE POLICY agent_dispatch_update ON agent_dispatches FOR UPDATE TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') OR EXISTS(SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id
          AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.agent_endpoint_eligible(a.id)))) WITH CHECK(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') AND state IN ('cancelled','expired') OR EXISTS(
          SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND hub.agent_endpoint_eligible(a.id)
          AND state IN ('received','accepted','declined')
          AND (run_id IS NULL OR EXISTS(SELECT 1 FROM task_runs r WHERE r.id=run_id AND r.user_id=a.user_id AND r.device_id=a.device_id)))));
    """)


def downgrade():
    raise RuntimeError("Preserve dispatch and turn history; restore a verified backup instead")
