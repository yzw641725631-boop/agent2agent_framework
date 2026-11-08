"""Versioned project rules, acknowledged work contexts and local change evidence."""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE tasks ADD COLUMN execution_mode varchar(24) NOT NULL DEFAULT 'declaration'
          CHECK (execution_mode IN ('observed','declaration'));
        ALTER TABLE worklogs ADD COLUMN context_id uuid;
        ALTER TABLE worklogs ADD COLUMN change_evidence jsonb;
        ALTER TABLE worklogs ADD COLUMN evidence_hash varchar(64);
        CREATE TABLE project_policies (
          project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
        );
        CREATE TABLE work_contexts (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, run_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          lease_epoch integer NOT NULL CHECK(lease_epoch>0), policy_version integer NOT NULL CHECK(policy_version>=0),
          task_hash varchar(64) NOT NULL, payload jsonb NOT NULL, content_hash varchar(64) NOT NULL,
          acknowledged_at timestamptz,
          FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id)
        );
        CREATE INDEX ix_contexts_run ON work_contexts(project_id,run_id,created_at);
        GRANT SELECT,INSERT ON project_policies TO hub_runtime;
        GRANT SELECT,INSERT ON work_contexts TO hub_runtime;
        GRANT UPDATE(acknowledged_at) ON work_contexts TO hub_runtime;
        ALTER TABLE project_policies ENABLE ROW LEVEL SECURITY;
        ALTER TABLE project_policies FORCE ROW LEVEL SECURITY;
        ALTER TABLE work_contexts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE work_contexts FORCE ROW LEVEL SECURITY;
        CREATE POLICY policies_read ON project_policies FOR SELECT TO hub_runtime
          USING (hub.can_access_project(project_id));
        CREATE POLICY policies_insert ON project_policies FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:publish')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
        CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
            OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY contexts_insert ON work_contexts FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND EXISTS (SELECT 1 FROM tasks t JOIN task_runs r ON t.active_run_id=r.id
            WHERE t.id=work_contexts.task_id AND t.project_id=work_contexts.project_id
              AND r.id=work_contexts.run_id AND r.state='active' AND t.lease_epoch=work_contexts.lease_epoch
              AND t.lease_until>clock_timestamp() AND r.user_id=work_contexts.author_id AND r.device_id=work_contexts.device_id));
        CREATE POLICY contexts_ack ON work_contexts FOR UPDATE TO hub_runtime USING (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid)
          WITH CHECK (hub.can_access_project(project_id));
    """)


def downgrade():
    raise RuntimeError("Preserve rules and context receipts; roll back the app image without dropping history")
