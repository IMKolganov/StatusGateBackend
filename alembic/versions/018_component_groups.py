"""component groups per project

Revision ID: 018
Revises: 017
Create Date: 2026-08-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "018"
down_revision: str | Sequence[str] | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "component_groups",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "slug", name="uq_component_groups_project_slug"),
    )
    op.create_index("ix_component_groups_project_id", "component_groups", ["project_id"], unique=False)
    op.create_index(
        "ix_component_groups_project_id_sort_order",
        "component_groups",
        ["project_id", "sort_order"],
        unique=False,
    )

    op.add_column(
        "monitored_components",
        sa.Column("group_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "monitored_components",
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_foreign_key(
        "fk_monitored_components_group_id",
        "monitored_components",
        "component_groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_monitored_components_group_id", "monitored_components", ["group_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_monitored_components_group_id", table_name="monitored_components")
    op.drop_constraint("fk_monitored_components_group_id", "monitored_components", type_="foreignkey")
    op.drop_column("monitored_components", "sort_order")
    op.drop_column("monitored_components", "group_id")
    op.drop_index("ix_component_groups_project_id_sort_order", table_name="component_groups")
    op.drop_index("ix_component_groups_project_id", table_name="component_groups")
    op.drop_table("component_groups")
