"""Add monitored_components.ip_family (auto|ipv4|ipv6).

Revision ID: 019
Revises: 018
Create Date: 2026-08-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | Sequence[str] | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitored_components",
        sa.Column(
            "ip_family",
            sa.String(length=10),
            server_default="auto",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("monitored_components", "ip_family")
