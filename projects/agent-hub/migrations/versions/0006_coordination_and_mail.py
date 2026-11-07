"""Bounded admission state, pending approvals and durable encrypted mail delivery."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE approval_requests SET status='expired'
          WHERE status='pending_approval' AND expires_at <= now();
        DELETE FROM rate_buckets WHERE resets_at < now();
        CREATE INDEX ix_approval_pending ON approval_requests(created_at, id)
          WHERE status='pending_approval';
        CREATE INDEX ix_mail_outbox_pending ON outbox_events(created_at)
          WHERE kind='mail.delivery' AND state IN ('pending', 'running');
    """)


def downgrade():
    op.execute("DROP INDEX ix_mail_outbox_pending")
    op.execute("DROP INDEX ix_approval_pending")
