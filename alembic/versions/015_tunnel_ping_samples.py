"""continuous tunnel ping samples

Revision ID: 015
Revises: 014
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "015"
down_revision: str | Sequence[str] | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tunnel_ping_samples",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("monitored_component_id", UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target", sa.String(length=20), nullable=False),
        sa.Column("target_host", sa.String(length=64), nullable=True),
        sa.Column("samples_sent", sa.Integer(), nullable=False),
        sa.Column("samples_received", sa.Integer(), nullable=False),
        sa.Column("loss_percent", sa.Float(), nullable=True),
        sa.Column("min_ms", sa.Float(), nullable=True),
        sa.Column("avg_ms", sa.Float(), nullable=True),
        sa.Column("max_ms", sa.Float(), nullable=True),
        sa.Column("jitter_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["monitored_component_id"],
            ["monitored_components.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "monitored_component_id",
            "target",
            "bucket_start",
            name="uq_tunnel_ping_samples_component_target_bucket",
        ),
    )
    op.create_index(
        "ix_tunnel_ping_samples_monitored_component_id",
        "tunnel_ping_samples",
        ["monitored_component_id"],
    )
    op.create_index("ix_tunnel_ping_samples_bucket_start", "tunnel_ping_samples", ["bucket_start"])
    op.create_check_constraint(
        "ck_tunnel_ping_samples_target",
        "tunnel_ping_samples",
        "target IN ('gateway', 'internet')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tunnel_ping_samples_target", "tunnel_ping_samples", type_="check")
    op.drop_index("ix_tunnel_ping_samples_bucket_start", table_name="tunnel_ping_samples")
    op.drop_index("ix_tunnel_ping_samples_monitored_component_id", table_name="tunnel_ping_samples")
    op.drop_table("tunnel_ping_samples")
