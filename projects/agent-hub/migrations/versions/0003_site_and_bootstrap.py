"""Encrypted site settings and one-time verified browser setup."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "email_challenges", sa.Column("purpose", sa.String(16), nullable=False, server_default="device")
    )
    op.add_column("email_challenges", sa.Column("user_epoch", sa.Integer()))
    op.create_table(
        "site_configuration",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "bootstrap_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("login", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("public_jwk", JSONB(), nullable=False),
        sa.Column("thumbprint", sa.String(64), nullable=False),
        sa.Column("device_name", sa.String(100), nullable=False),
        sa.Column("os", sa.String(16), nullable=False),
        sa.Column("configuration_version", sa.Integer(), nullable=False),
        sa.Column("challenge_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_bootstrap_attempts_thumbprint", "bootstrap_attempts", ["thumbprint"])
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON site_configuration, bootstrap_attempts TO hub_runtime"
    )


def downgrade():
    op.drop_table("bootstrap_attempts")
    op.drop_table("site_configuration")
    op.drop_column("email_challenges", "user_epoch")
    op.drop_column("email_challenges", "purpose")
