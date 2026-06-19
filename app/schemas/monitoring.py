from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CheckResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    monitored_component_id: UUID
    checked_at: datetime
    outcome: str
    latency_ms: int | None
    http_status_code: int | None
    error_message: str | None
    details: dict | None


class MonitoringSettingsResponse(BaseModel):
    default_poll_interval_seconds: int
    scheduler_interval_seconds: int
    updated_at: datetime


class MonitoringSettingsUpdate(BaseModel):
    default_poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    scheduler_interval_seconds: int | None = Field(default=None, ge=5, le=3600)


class PurgeCheckHistoryResponse(BaseModel):
    deleted_count: int
    remaining_count: int
