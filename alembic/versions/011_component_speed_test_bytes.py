"""component speed test bytes

Revision ID: 011
Revises: 010
Create Date: 2026-06-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | Sequence[str] | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitored_components",
        sa.Column("speed_test_bytes", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_monitored_components_speed_test_bytes",
        "monitored_components",
        "speed_test_bytes IS NULL OR (speed_test_bytes >= 1024 AND speed_test_bytes <= 52428800)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_monitored_components_speed_test_bytes", "monitored_components", type_="check")
    op.drop_column("monitored_components", "speed_test_bytes")
