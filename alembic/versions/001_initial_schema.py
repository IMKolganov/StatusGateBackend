"""initial schema: component_kinds, projects, monitored_components

Revision ID: 001
Revises:
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "component_kinds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_component_kinds_slug", "component_kinds", ["slug"], unique=False)

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_projects_slug", "projects", ["slug"], unique=False)

    op.create_table(
        "monitored_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("component_kind_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("environment", sa.String(length=50), nullable=True),
        sa.Column("check_url", sa.String(length=2048), nullable=False),
        sa.Column("check_method", sa.String(length=10), server_default="GET", nullable=False),
        sa.Column("expected_status_code", sa.Integer(), server_default="200", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="10", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["component_kind_id"], ["component_kinds.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "slug", name="uq_monitored_components_project_slug"),
    )
    op.create_index("ix_monitored_components_component_kind_id", "monitored_components", ["component_kind_id"], unique=False)
    op.create_index("ix_monitored_components_environment", "monitored_components", ["environment"], unique=False)
    op.create_index("ix_monitored_components_project_id", "monitored_components", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_monitored_components_project_id", table_name="monitored_components")
    op.drop_index("ix_monitored_components_environment", table_name="monitored_components")
    op.drop_index("ix_monitored_components_component_kind_id", table_name="monitored_components")
    op.drop_table("monitored_components")

    op.drop_index("ix_projects_slug", table_name="projects")
    op.drop_table("projects")

    op.drop_index("ix_component_kinds_slug", table_name="component_kinds")
    op.drop_table("component_kinds")
