from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import VPN_CHECK_TYPES
from app.schemas.network import NetworkSummary, VpnCheckConfig
from app.services.speed_test_config import validate_speed_test_url_template

DEFAULT_SPEED_TEST_BYTES = 524_288
MIN_SPEED_TEST_BYTES = 1_024
MAX_SPEED_TEST_BYTES = 52_428_800
MIN_SPEED_TEST_INTERVAL_SECONDS = 0
MAX_SPEED_TEST_INTERVAL_SECONDS = 86_400


CHECK_TYPE_PATTERN = r"^(http_status|json|xml|openvpn|xray)$"


class MonitoredComponentCreate(BaseModel):
    project_id: UUID
    component_kind_id: UUID
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    environment: str | None = Field(default=None, max_length=50)
    check_url: str = Field(default="", max_length=2048)
    check_method: str = Field(default="GET", max_length=10)
    check_type: str = Field(default="http_status", pattern=CHECK_TYPE_PATTERN)
    check_config: VpnCheckConfig | None = None
    speed_test_bytes: int | None = Field(default=None, ge=MIN_SPEED_TEST_BYTES, le=MAX_SPEED_TEST_BYTES)
    speed_test_url_template: str | None = Field(default=None, max_length=2048)
    speed_test_interval_seconds: int | None = Field(
        default=None,
        ge=MIN_SPEED_TEST_INTERVAL_SECONDS,
        le=MAX_SPEED_TEST_INTERVAL_SECONDS,
    )
    speed_test_enabled: bool | None = None
    expected_status_code: int = Field(default=200, ge=100, le=599)
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_check_fields(self) -> "MonitoredComponentCreate":
        if self.check_type in VPN_CHECK_TYPES:
            if not self.check_config:
                raise ValueError("check_config is required for VPN check types")
            VpnCheckConfig.model_validate(self.check_config)
            if not self.check_url.strip():
                self.check_url = "https://ifconfig.me/ip"
            if self.timeout_seconds < 30:
                self.timeout_seconds = 30
            if self.speed_test_url_template is not None:
                self.speed_test_url_template = validate_speed_test_url_template(self.speed_test_url_template)
            return self

        if not self.check_url.strip():
            raise ValueError("check_url is required for HTTP check types")
        if self.check_config:
            raise ValueError("check_config is only supported for VPN check types")
        if self.speed_test_bytes is not None:
            raise ValueError("speed_test_bytes is only supported for VPN check types")
        if self.speed_test_url_template is not None:
            raise ValueError("speed_test_url_template is only supported for VPN check types")
        if self.speed_test_interval_seconds is not None:
            raise ValueError("speed_test_interval_seconds is only supported for VPN check types")
        if self.speed_test_enabled is not None:
            raise ValueError("speed_test_enabled is only supported for VPN check types")
        return self


class MonitoredComponentUpdate(BaseModel):
    project_id: UUID | None = None
    component_kind_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    environment: str | None = Field(default=None, max_length=50)
    check_url: str | None = Field(default=None, max_length=2048)
    check_method: str | None = Field(default=None, max_length=10)
    check_type: str | None = Field(default=None, pattern=CHECK_TYPE_PATTERN)
    check_config: VpnCheckConfig | None = None
    speed_test_bytes: int | None = Field(default=None, ge=MIN_SPEED_TEST_BYTES, le=MAX_SPEED_TEST_BYTES)
    speed_test_url_template: str | None = Field(default=None, max_length=2048)
    speed_test_interval_seconds: int | None = Field(
        default=None,
        ge=MIN_SPEED_TEST_INTERVAL_SECONDS,
        le=MAX_SPEED_TEST_INTERVAL_SECONDS,
    )
    speed_test_enabled: bool | None = None
    expected_status_code: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    poll_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_check_fields(self) -> "MonitoredComponentUpdate":
        if self.check_type in VPN_CHECK_TYPES or self.check_config is not None:
            if self.check_config is not None:
                VpnCheckConfig.model_validate(self.check_config)
        if self.speed_test_url_template is not None:
            self.speed_test_url_template = validate_speed_test_url_template(self.speed_test_url_template)
        return self


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
    check_config: VpnCheckConfig | None
    speed_test_bytes: int | None
    speed_test_url_template: str | None
    speed_test_interval_seconds: int | None
    speed_test_enabled: bool
    expected_status_code: int
    timeout_seconds: int
    poll_interval_seconds: int | None
    last_checked_at: datetime | None
    is_active: bool
    latest_outcome: str | None = None
    latest_latency_ms: int | None = None
    latest_checked_at: datetime | None = None
    latest_error_message: str | None = None
    latest_log_tail: str | None = None
    latest_network_summary: NetworkSummary | None = None
    created_at: datetime
    updated_at: datetime
