"""accounts, access_roles, account_roles

Revision ID: 002
Revises: 001
Create Date: 2026-06-16

"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | Sequence[str] | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_ROLE_ID = UUID("00000000-0000-4000-8000-000000000001")
OPERATOR_ROLE_ID = UUID("00000000-0000-4000-8000-000000000002")
VIEWER_ROLE_ID = UUID("00000000-0000-4000-8000-000000000003")

access_roles_table = sa.table(
    "access_roles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.String),
    sa.column("slug", sa.String),
    sa.column("description", sa.Text),
)


def upgrade() -> None:
    op.create_table(
        "access_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_access_roles_slug", "access_roles", ["slug"], unique=False)

    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("google_sub", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("totp_secret", sa.String(length=64), nullable=True),
        sa.Column("is_totp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("google_sub"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_accounts_email", "accounts", ["email"], unique=False)
    op.create_index("ix_accounts_google_sub", "accounts", ["google_sub"], unique=False)
    op.create_index("ix_accounts_username", "accounts", ["username"], unique=False)

    op.create_table(
        "account_roles",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["access_role_id"], ["access_roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "access_role_id"),
    )

    op.bulk_insert(
        access_roles_table,
        [
            {
                "id": ADMIN_ROLE_ID,
                "name": "Administrator",
                "slug": "admin",
                "description": "Full access to management panel",
            },
            {
                "id": OPERATOR_ROLE_ID,
                "name": "Operator",
                "slug": "operator",
                "description": "Can manage projects and monitored components",
            },
            {
                "id": VIEWER_ROLE_ID,
                "name": "Viewer",
                "slug": "viewer",
                "description": "Read-only access",
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("account_roles")
    op.drop_index("ix_accounts_username", table_name="accounts")
    op.drop_index("ix_accounts_google_sub", table_name="accounts")
    op.drop_index("ix_accounts_email", table_name="accounts")
    op.drop_table("accounts")
    op.drop_index("ix_access_roles_slug", table_name="access_roles")
    op.drop_table("access_roles")
