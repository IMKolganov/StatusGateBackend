from __future__ import annotations

from uuid import UUID

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel
from app.core.speed_test_defaults import DEFAULT_SPEED_TEST_INTERVAL_SECONDS, DEFAULT_SPEED_TEST_URL_TEMPLATE

MONITORING_SETTINGS_ID = UUID("00000000-0000-4000-8000-000000000010")


class MonitoringSettings(BaseModel[UUID]):
    __tablename__ = "monitoring_settings"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    default_poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    scheduler_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    default_speed_test_url_template: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        default=DEFAULT_SPEED_TEST_URL_TEMPLATE,
        server_default=DEFAULT_SPEED_TEST_URL_TEMPLATE,
    )
    default_speed_test_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_SPEED_TEST_INTERVAL_SECONDS,
        server_default=str(DEFAULT_SPEED_TEST_INTERVAL_SECONDS),
    )
