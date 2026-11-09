"""Project occupations and explicit task workflow snapshots."""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE project_collaboration (
        project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
      );
      ALTER TABLE project_collaboration ENABLE ROW LEVEL SECURITY;
      ALTER TABLE project_collaboration FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT ON project_collaboration TO hub_runtime;
      CREATE POLICY collaboration_owner ON project_collaboration TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY collaboration_read ON project_collaboration FOR SELECT TO hub_runtime
        USING(hub.can_access_project(project_id));
      CREATE POLICY collaboration_insert ON project_collaboration FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      ALTER TABLE tasks ADD COLUMN role_profile varchar(64);
      ALTER TABLE tasks ADD COLUMN workflow varchar(16) NOT NULL DEFAULT 'standard'
        CHECK(workflow IN ('standard','compact','strict'));
      ALTER TABLE tasks ADD COLUMN workflow_reason varchar(1000) NOT NULL DEFAULT '';
      ALTER TABLE tasks ADD COLUMN workflow_policy jsonb NOT NULL DEFAULT '{}';
      GRANT UPDATE(workflow,workflow_reason,workflow_policy) ON tasks TO hub_runtime;
      ALTER TABLE agent_endpoints ADD COLUMN role_profiles jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(role_profiles)='array' AND jsonb_array_length(role_profiles)<=32);
      GRANT UPDATE(role_profiles) ON agent_endpoints TO hub_runtime;
    """)


def downgrade():
    raise RuntimeError("Preserve role and workflow history; restore from a verified backup")
