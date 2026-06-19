"""vpn check config and check types

Revision ID: 010
Revises: 009
Create Date: 2026-06-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: str | Sequence[str] | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("monitored_components", sa.Column("check_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_constraint("ck_monitored_components_check_type", "monitored_components", type_="check")
    op.create_check_constraint(
        "ck_monitored_components_check_type",
        "monitored_components",
        "check_type IN ('http_status', 'json', 'xml', 'openvpn', 'xray')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_monitored_components_check_type", "monitored_components", type_="check")
    op.create_check_constraint(
        "ck_monitored_components_check_type",
        "monitored_components",
        "check_type IN ('http_status', 'json', 'xml')",
    )
    op.drop_column("monitored_components", "check_config")
