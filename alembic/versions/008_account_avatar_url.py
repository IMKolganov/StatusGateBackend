"""account avatar url

Revision ID: 008
Revises: 007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("avatar_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "avatar_url")
