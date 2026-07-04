"""connection mode for VPN monitoring

Revision ID: 013
Revises: 012
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str | Sequence[str] | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitored_components",
        sa.Column(
            "connection_mode",
            sa.String(length=20),
            nullable=False,
            server_default="ephemeral",
        ),
    )
    op.create_check_constraint(
        "ck_monitored_components_connection_mode",
        "monitored_components",
        "connection_mode IN ('ephemeral', 'persistent')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_monitored_components_connection_mode", "monitored_components", type_="check")
    op.drop_column("monitored_components", "connection_mode")
