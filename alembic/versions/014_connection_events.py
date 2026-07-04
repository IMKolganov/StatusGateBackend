"""connection events timeline

Revision ID: 014
Revises: 013
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "014"
down_revision: str | Sequence[str] | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connection_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("monitored_component_id", UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["monitored_component_id"],
            ["monitored_components.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_connection_events_monitored_component_id", "connection_events", ["monitored_component_id"])
    op.create_index("ix_connection_events_occurred_at", "connection_events", ["occurred_at"])
    op.create_index("ix_connection_events_event_type", "connection_events", ["event_type"])
    op.create_check_constraint(
        "ck_connection_events_event_type",
        "connection_events",
        "event_type IN ('tunnel_up', 'tunnel_down', 'reconnect', 'connect_failed', 'unavailable', 'available')",
    )

    op.execute(
        sa.text(
            """
            INSERT INTO connection_events (
                id,
                monitored_component_id,
                occurred_at,
                event_type,
                outcome,
                message,
                details,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                monitored_component_id,
                checked_at,
                details->>'session_event',
                outcome,
                error_message,
                details,
                checked_at,
                checked_at
            FROM check_results
            WHERE details->>'session_event' IS NOT NULL
              AND details->>'session_event' <> 'probe'
              AND details->>'session_event' IN (
                  'tunnel_up', 'tunnel_down', 'reconnect', 'connect_failed', 'unavailable', 'available'
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_connection_events_event_type", "connection_events", type_="check")
    op.drop_index("ix_connection_events_event_type", table_name="connection_events")
    op.drop_index("ix_connection_events_occurred_at", table_name="connection_events")
    op.drop_index("ix_connection_events_monitored_component_id", table_name="connection_events")
    op.drop_table("connection_events")
