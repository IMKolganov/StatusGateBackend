from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.models.project import Project

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class DatagateIntegration(Base, TimestampMixin):
    """Per-project credentials for the DataGate Monitor API."""

    __tablename__ = "datagate_integrations"

    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    base_url: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        default="https://api.datagateapp.com",
        server_default="https://api.datagateapp.com",
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)
    monitor_cn_prefix: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="statusgate",
        server_default="statusgate",
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    project: Mapped["Project"] = relationship(back_populates="datagate_integration")
