"""add user access role for public accounts

Revision ID: 004
Revises: 003
Create Date: 2026-06-16

"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: str | Sequence[str] | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ROLE_ID = UUID("00000000-0000-4000-8000-000000000004")

access_roles_table = sa.table(
    "access_roles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.String),
    sa.column("slug", sa.String),
    sa.column("description", sa.Text),
)


def upgrade() -> None:
    op.bulk_insert(
        access_roles_table,
        [
            {
                "id": USER_ROLE_ID,
                "name": "User",
                "slug": "user",
                "description": "Public account — can subscribe to status updates",
            },
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM account_roles WHERE access_role_id = :role_id").bindparams(role_id=USER_ROLE_ID)
    )
    op.execute(sa.text("DELETE FROM access_roles WHERE id = :role_id").bindparams(role_id=USER_ROLE_ID))
