"""speed test settings

Revision ID: 012
Revises: 011
Create Date: 2026-06-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str | Sequence[str] | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_SPEED_TEST_URL_TEMPLATE = "https://speed.cloudflare.com/__down?bytes={bytes}"


def upgrade() -> None:
    op.add_column(
        "monitoring_settings",
        sa.Column(
            "default_speed_test_url_template",
            sa.String(length=2048),
            nullable=False,
            server_default=DEFAULT_SPEED_TEST_URL_TEMPLATE,
        ),
    )
    op.add_column(
        "monitoring_settings",
        sa.Column(
            "default_speed_test_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="3600",
        ),
    )
    op.add_column(
        "monitored_components",
        sa.Column("speed_test_url_template", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "monitored_components",
        sa.Column("speed_test_interval_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "monitored_components",
        sa.Column("speed_test_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_check_constraint(
        "ck_monitored_components_speed_test_interval_seconds",
        "monitored_components",
        "speed_test_interval_seconds IS NULL OR speed_test_interval_seconds >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_monitored_components_speed_test_interval_seconds", "monitored_components", type_="check")
    op.drop_column("monitored_components", "speed_test_enabled")
    op.drop_column("monitored_components", "speed_test_interval_seconds")
    op.drop_column("monitored_components", "speed_test_url_template")
    op.drop_column("monitoring_settings", "default_speed_test_interval_seconds")
    op.drop_column("monitoring_settings", "default_speed_test_url_template")
