"""incident service binding and date range

Revision ID: 017
Revises: 016
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "017"
down_revision: str | Sequence[str] | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column("monitored_component_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_incidents_monitored_component_id",
        "incidents",
        "monitored_components",
        ["monitored_component_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_incidents_monitored_component_id",
        "incidents",
        ["monitored_component_id"],
        unique=False,
    )
    op.create_index("ix_incidents_starts_at", "incidents", ["starts_at"], unique=False)

    op.execute(
        sa.text(
            """
            UPDATE incidents AS i
            SET
                starts_at = COALESCE(
                    (
                        SELECT MIN(u.posted_at)
                        FROM incident_updates AS u
                        WHERE u.incident_id = i.id
                    ),
                    i.created_at
                ),
                ends_at = COALESCE(
                    (
                        SELECT MIN(u.posted_at)
                        FROM incident_updates AS u
                        WHERE u.incident_id = i.id
                    ),
                    i.created_at
                )
            WHERE i.starts_at IS NULL
            """
        )
    )
    op.alter_column("incidents", "starts_at", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_incidents_starts_at", table_name="incidents")
    op.drop_index("ix_incidents_monitored_component_id", table_name="incidents")
    op.drop_constraint("fk_incidents_monitored_component_id", "incidents", type_="foreignkey")
    op.drop_column("incidents", "ends_at")
    op.drop_column("incidents", "starts_at")
    op.drop_column("incidents", "monitored_component_id")
