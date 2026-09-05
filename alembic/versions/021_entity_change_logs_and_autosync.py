"""Add entity_change_logs and DataGate auto-sync columns.

Revision ID: 021
Revises: 020
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021"
down_revision: str | Sequence[str] | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_change_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=30), server_default="system", nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace_id", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["actor_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entity_change_logs_occurred_at", "entity_change_logs", ["occurred_at"])
    op.create_index("ix_entity_change_logs_entity_type", "entity_change_logs", ["entity_type"])
    op.create_index("ix_entity_change_logs_entity_id", "entity_change_logs", ["entity_id"])
    op.create_index("ix_entity_change_logs_batch_id", "entity_change_logs", ["batch_id"])
    op.create_index("ix_entity_change_logs_project_id", "entity_change_logs", ["project_id"])
    op.create_index("ix_entity_change_logs_actor_account_id", "entity_change_logs", ["actor_account_id"])
    op.create_index(
        "ix_entity_change_logs_project_occurred",
        "entity_change_logs",
        ["project_id", "occurred_at"],
    )

    op.add_column(
        "datagate_integrations",
        sa.Column("auto_sync_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "datagate_integrations",
        sa.Column("auto_sync_interval_hours", sa.Integer(), server_default="24", nullable=False),
    )
    op.add_column(
        "datagate_integrations",
        sa.Column("auto_sync_import_new", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "datagate_integrations",
        sa.Column("auto_sync_deactivate_removed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("datagate_integrations", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("datagate_integrations", sa.Column("last_sync_status", sa.String(length=50), nullable=True))
    op.add_column("datagate_integrations", sa.Column("last_sync_error", sa.Text(), nullable=True))
    op.add_column(
        "datagate_integrations",
        sa.Column("last_sync_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("datagate_integrations", "last_sync_batch_id")
    op.drop_column("datagate_integrations", "last_sync_error")
    op.drop_column("datagate_integrations", "last_sync_status")
    op.drop_column("datagate_integrations", "last_synced_at")
    op.drop_column("datagate_integrations", "auto_sync_deactivate_removed")
    op.drop_column("datagate_integrations", "auto_sync_import_new")
    op.drop_column("datagate_integrations", "auto_sync_interval_hours")
    op.drop_column("datagate_integrations", "auto_sync_enabled")

    op.drop_index("ix_entity_change_logs_project_occurred", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_actor_account_id", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_project_id", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_batch_id", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_entity_id", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_entity_type", table_name="entity_change_logs")
    op.drop_index("ix_entity_change_logs_occurred_at", table_name="entity_change_logs")
    op.drop_table("entity_change_logs")
