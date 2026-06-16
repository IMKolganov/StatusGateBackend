from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class MonitoredComponentCreate(BaseModel):
    project_id: UUID
    component_kind_id: UUID
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    environment: str | None = Field(default=None, max_length=50)
    check_url: str = Field(min_length=1, max_length=2048)
    check_method: str = Field(default="GET", max_length=10)
    check_type: str = Field(default="http_status", pattern=r"^(http_status|json|xml)$")
    expected_status_code: int = Field(default=200, ge=100, le=599)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    is_active: bool = True


class MonitoredComponentUpdate(BaseModel):
    project_id: UUID | None = None
    component_kind_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    environment: str | None = Field(default=None, max_length=50)
    check_url: str | None = Field(default=None, min_length=1, max_length=2048)
    check_method: str | None = Field(default=None, max_length=10)
    check_type: str | None = Field(default=None, pattern=r"^(http_status|json|xml)$")
    expected_status_code: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    is_active: bool | None = None


class MonitoredComponentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    component_kind_id: UUID
    name: str
    slug: str
    description: str | None
    environment: str | None
    check_url: str
    check_method: str
    check_type: str
    expected_status_code: int
    timeout_seconds: int
    poll_interval_seconds: int | None
    last_checked_at: datetime | None
    is_active: bool
    latest_outcome: str | None = None
    latest_latency_ms: int | None = None
    latest_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
