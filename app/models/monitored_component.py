from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel
from app.models.enums import CheckType


class MonitoredComponent(BaseModel[UUID]):
    __tablename__ = "monitored_components"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_monitored_components_project_slug"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    component_kind_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("component_kinds.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    check_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    check_method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET", server_default="GET")
    expected_status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200, server_default="200")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    check_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=CheckType.HTTP_STATUS.value,
        server_default=CheckType.HTTP_STATUS.value,
    )
    check_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    speed_test_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_test_url_template: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    speed_test_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_test_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    project: Mapped["Project"] = relationship(back_populates="monitored_components")
    component_kind: Mapped["ComponentKind"] = relationship()
    check_results: Mapped[list["CheckResult"]] = relationship(
        back_populates="monitored_component",
        cascade="all, delete-orphan",
    )
