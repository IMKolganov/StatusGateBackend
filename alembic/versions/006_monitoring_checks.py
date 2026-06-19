"""monitoring check types, poll intervals, settings

Revision ID: 006
Revises: 005
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: str | Sequence[str] | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONITORING_SETTINGS_ID = "00000000-0000-4000-8000-000000000010"


def upgrade() -> None:
    op.add_column(
        "monitored_components",
        sa.Column("check_type", sa.String(length=20), server_default="http_status", nullable=False),
    )
    op.add_column(
        "monitored_components",
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "monitored_components",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_monitored_components_last_checked_at",
        "monitored_components",
        ["last_checked_at"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_monitored_components_check_type",
        "monitored_components",
        "check_type IN ('http_status', 'json', 'xml')",
    )
    op.create_check_constraint(
        "ck_monitored_components_poll_interval",
        "monitored_components",
        "poll_interval_seconds IS NULL OR poll_interval_seconds >= 10",
    )

    op.create_table(
        "monitoring_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("default_poll_interval_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("scheduler_interval_seconds", sa.Integer(), server_default="30", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("default_poll_interval_seconds >= 10", name="ck_monitoring_settings_default_poll"),
        sa.CheckConstraint("scheduler_interval_seconds >= 5", name="ck_monitoring_settings_scheduler"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO monitoring_settings (id, default_poll_interval_seconds, scheduler_interval_seconds) "
            "VALUES (:id, 60, 30)"
        ),
        {"id": MONITORING_SETTINGS_ID},
    )


def downgrade() -> None:
    op.drop_table("monitoring_settings")
    op.drop_index("ix_monitored_components_last_checked_at", table_name="monitored_components")
    op.drop_constraint("ck_monitored_components_poll_interval", "monitored_components", type_="check")
    op.drop_constraint("ck_monitored_components_check_type", "monitored_components", type_="check")
    op.drop_column("monitored_components", "last_checked_at")
    op.drop_column("monitored_components", "poll_interval_seconds")
    op.drop_column("monitored_components", "check_type")
