"""covering indexes for public status queries

Revision ID: 016
Revises: 015
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str | Sequence[str] | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Day-stats queries only need (component, checked_at, outcome); INCLUDE enables
    # index-only scans and avoids heap fetches for ~90d home/system-status loads.
    op.execute(sa.text("DROP INDEX IF EXISTS ix_check_results_component_checked_at_outcome"))
    op.drop_index("ix_check_results_component_checked_at", table_name="check_results")
    op.create_index(
        "ix_check_results_component_checked_at",
        "check_results",
        ["monitored_component_id", "checked_at"],
        unique=False,
        postgresql_include=["outcome"],
    )

    op.create_index(
        "ix_connection_events_component_occurred_at",
        "connection_events",
        ["monitored_component_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_tunnel_ping_samples_component_bucket_start",
        "tunnel_ping_samples",
        ["monitored_component_id", "bucket_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tunnel_ping_samples_component_bucket_start",
        table_name="tunnel_ping_samples",
    )
    op.drop_index(
        "ix_connection_events_component_occurred_at",
        table_name="connection_events",
    )
    op.drop_index("ix_check_results_component_checked_at", table_name="check_results")
    op.create_index(
        "ix_check_results_component_checked_at",
        "check_results",
        ["monitored_component_id", "checked_at"],
        unique=False,
    )
