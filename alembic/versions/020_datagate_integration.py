"""Add DataGate integration table and component linkage columns.

Revision ID: 020
Revises: 019
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: str | Sequence[str] | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datagate_integrations",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False, server_default="https://api.datagateapp.com"),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
        sa.Column("monitor_cn_prefix", sa.String(length=100), nullable=False, server_default="statusgate"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
    )

    op.add_column("monitored_components", sa.Column("datagate_server_id", sa.Integer(), nullable=True))
    op.add_column(
        "monitored_components",
        sa.Column("datagate_common_name", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_monitored_components_datagate_server_id",
        "monitored_components",
        ["datagate_server_id"],
    )
    op.create_index(
        "uq_monitored_components_project_datagate_server",
        "monitored_components",
        ["project_id", "datagate_server_id"],
        unique=True,
        postgresql_where=sa.text("datagate_server_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_monitored_components_project_datagate_server",
        table_name="monitored_components",
    )
    op.drop_index("ix_monitored_components_datagate_server_id", table_name="monitored_components")
    op.drop_column("monitored_components", "datagate_common_name")
    op.drop_column("monitored_components", "datagate_server_id")
    op.drop_table("datagate_integrations")
