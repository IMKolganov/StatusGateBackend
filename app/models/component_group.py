from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.models.monitored_component import MonitoredComponent
    from app.models.project import Project

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class ComponentGroup(BaseModel[UUID]):
    __tablename__ = "component_groups"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_component_groups_project_slug"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    project: Mapped["Project"] = relationship(back_populates="component_groups")
    monitored_components: Mapped[list["MonitoredComponent"]] = relationship(back_populates="group")
