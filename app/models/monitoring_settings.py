from uuid import UUID

from sqlalchemy import Integer
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel

MONITORING_SETTINGS_ID = UUID("00000000-0000-4000-8000-000000000010")


class MonitoringSettings(BaseModel[UUID]):
    __tablename__ = "monitoring_settings"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    default_poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    scheduler_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
