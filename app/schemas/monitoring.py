from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.speed_test_config import validate_speed_test_url_template


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
    default_speed_test_url_template: str
    default_speed_test_interval_seconds: int
    updated_at: datetime


class MonitoringSettingsUpdate(BaseModel):
    default_poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    scheduler_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    default_speed_test_url_template: str | None = Field(default=None, max_length=2048)
    default_speed_test_interval_seconds: int | None = Field(default=None, ge=0, le=86400)

    @field_validator("default_speed_test_url_template")
    @classmethod
    def validate_default_speed_test_url_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_speed_test_url_template(value)


class SpeedTestAdvisoryResponse(BaseModel):
    active_vpn_service_count: int
    estimated_speed_tests_per_minute: float
    uses_default_cloudflare_template: bool
    guidance_requests_per_minute: int
    warning: str | None


class PurgeCheckHistoryResponse(BaseModel):
    deleted_count: int
    remaining_count: int
