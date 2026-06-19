"""account email verification fields

Revision ID: 005
Revises: 004
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | Sequence[str] | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("is_email_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "accounts",
        sa.Column("email_verification_token", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_accounts_email_verification_token", "accounts", ["email_verification_token"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_accounts_email_verification_token", table_name="accounts")
    op.drop_column("accounts", "email_verification_token")
    op.drop_column("accounts", "is_email_verified")
