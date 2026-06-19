"""check_results, subscriptions, refresh_tokens

Revision ID: 003
Revises: 002
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: str | Sequence[str] | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "check_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("monitored_component_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["monitored_component_id"], ["monitored_components.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_check_results_checked_at", "check_results", ["checked_at"], unique=False)
    op.create_index("ix_check_results_component_checked_at", "check_results", ["monitored_component_id", "checked_at"], unique=False)
    op.create_index("ix_check_results_monitored_component_id", "check_results", ["monitored_component_id"], unique=False)
    op.create_index("ix_check_results_outcome", "check_results", ["outcome"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination", sa.String(length=2048), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("monitored_component_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verification_token", sa.String(length=128), nullable=True),
        sa.Column("webhook_secret", sa.String(length=128), nullable=True),
        sa.Column("notify_on_incident", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notify_on_resolution", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notify_on_maintenance", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(project_id IS NOT NULL AND monitored_component_id IS NULL) OR "
            "(project_id IS NULL AND monitored_component_id IS NOT NULL)",
            name="ck_subscriptions_exactly_one_scope",
        ),
        sa.CheckConstraint("channel IN ('email', 'sms', 'webhook')", name="ck_subscriptions_channel"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["monitored_component_id"], ["monitored_components.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_account_id", "subscriptions", ["account_id"], unique=False)
    op.create_index("ix_subscriptions_channel", "subscriptions", ["channel"], unique=False)
    op.create_index("ix_subscriptions_monitored_component_id", "subscriptions", ["monitored_component_id"], unique=False)
    op.create_index("ix_subscriptions_project_id", "subscriptions", ["project_id"], unique=False)
    op.create_index("ix_subscriptions_verification_token", "subscriptions", ["verification_token"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_account_id", "refresh_tokens", ["account_id"], unique=False)
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"], unique=False)
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_account_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_subscriptions_verification_token", table_name="subscriptions")
    op.drop_index("ix_subscriptions_project_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_monitored_component_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_channel", table_name="subscriptions")
    op.drop_index("ix_subscriptions_account_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_check_results_outcome", table_name="check_results")
    op.drop_index("ix_check_results_monitored_component_id", table_name="check_results")
    op.drop_index("ix_check_results_component_checked_at", table_name="check_results")
    op.drop_index("ix_check_results_checked_at", table_name="check_results")
    op.drop_table("check_results")
