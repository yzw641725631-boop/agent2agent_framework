"""Durable project progress with project and writer isolation."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_progress",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("author_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_id", sa.UUID(), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("task_reference", sa.String(128), nullable=False),
        sa.Column("git_commit", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('running','checkpoint','blocked','completed')"),
    )
    op.create_index("ix_project_progress_project_id", "project_progress", ["project_id", "sequence"])
    op.execute("GRANT SELECT, INSERT ON project_progress TO hub_runtime")
    op.execute("GRANT USAGE ON SEQUENCE project_progress_sequence_seq TO hub_runtime")
    op.execute("ALTER TABLE project_progress ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_progress FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY progress_read ON project_progress FOR SELECT "
        "USING (hub.can_access_project(project_id))"
    )
    op.execute("""
        CREATE POLICY progress_insert ON project_progress FOR INSERT WITH CHECK (
          hub.can_access_project(project_id)
          AND author_id = nullif(current_setting('hub.user_id', true), '')::uuid
          AND device_id = nullif(current_setting('hub.device_id', true), '')::uuid
          AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id = project_progress.project_id AND g.capabilities ? 'tasks:write'
          ))
        )
    """)


def downgrade():
    op.drop_table("project_progress")
