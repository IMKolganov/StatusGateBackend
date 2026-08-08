from datetime import date, datetime
from uuid import UUID

from app.schemas.network import NetworkSummary
from pydantic import BaseModel, ConfigDict, Field


class PublicProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    description: str | None
    uptime_percent: float | None = None


class PublicServiceStatus(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    environment: str | None
    component_kind: str
    status: str = Field(description="Latest check outcome or 'unknown'")
    latency_ms: int | None = None
    checked_at: datetime | None = None
    network_summary: NetworkSummary | None = None


class PublicProjectStatus(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    services: list[PublicServiceStatus]


class PublicDayIncident(BaseModel):
    title: str
    message: str
    status: str
    posted_at: datetime
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    service_name: str | None = None
    service_slug: str | None = None


class PublicDayBar(BaseModel):
    date: date
    status: str = Field(description="operational, degraded, outage, or no_data")
    tooltip: str
    check_count: int = 0
    failed_count: int = 0
    degraded_count: int = 0
    availability_percent: float | None = None
    downtime_seconds: int = 0
    incidents: list[PublicDayIncident] = []


class PublicServiceTimeline(BaseModel):
    id: UUID
    name: str
    slug: str
    component_kind: str
    uptime_percent: float | None = None
    days: list[PublicDayBar]


class PublicComponentGroupTimeline(BaseModel):
    name: str
    component_count: int
    uptime_percent: float | None = None
    days: list[PublicDayBar]
    services: list[PublicServiceTimeline]


class PublicActiveAlert(BaseModel):
    title: str
    message: str
    status: str
    since: datetime | None = None


class PublicSystemStatus(BaseModel):
    project_id: UUID
    project_name: str
    project_slug: str
    range_start: date
    range_end: date
    range_label: str
    days: int
    groups: list[PublicComponentGroupTimeline]
    active_alerts: list[PublicActiveAlert] = []


class PublicTunnelMetricPoint(BaseModel):
    checked_at: datetime
    outcome: str
    latency_ms: int | None = None
    connect_time_ms: int | None = None
    exit_ip: str | None = None
    probe_latency_ms: float | None = None
    google_probe_ok: bool | None = None
    google_probe_latency_ms: float | None = None
    gateway_ping_avg_ms: float | None = None
    gateway_ping_jitter_ms: float | None = None
    gateway_ping_loss_percent: float | None = None
    download_mbps: float | None = None
    download_bytes: int | None = None
    download_duration_ms: int | None = None
    download_cached: bool | None = None
    speed_test_ok: bool | None = None
    speed_test_measured_at: str | None = None


class PublicTunnelPingSample(BaseModel):
    """One minute of the continuous in-tunnel pinger (1 packet/second)."""

    bucket_start: datetime
    target: str = Field(description="'gateway' (first VPN hop) or 'internet' (host beyond the exit)")
    target_host: str | None = None
    samples_sent: int
    samples_received: int
    loss_percent: float | None = None
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    jitter_ms: float | None = None


class PublicTunnelLatestDiagnostics(BaseModel):
    """Public-safe snapshot of the newest check — full tunnel diagnostics, not ping-only."""

    checked_at: datetime | None = None
    outcome: str | None = None
    exit_ip: str | None = None
    connect_time_ms: int | None = None
    probe_latency_ms: float | None = None
    google_probe_ok: bool | None = None
    google_probe_latency_ms: float | None = None
    gateway_ping_avg_ms: float | None = None
    gateway_ping_jitter_ms: float | None = None
    gateway_ping_loss_percent: float | None = None
    download_mbps: float | None = None
    download_bytes: int | None = None
    download_duration_ms: int | None = None
    speed_test_ok: bool | None = None
    speed_test_error: str | None = None
    speed_test_measured_at: str | None = None
    speed_test_last_success_at: str | None = None
    speed_test_showing_last_success: bool | None = None
    speed_test_min_mbps: float | None = None
    speed_test_max_mbps: float | None = None
    speed_test_avg_mbps: float | None = None
    speed_test_sample_count: int | None = None
    fresh_speed_tests_in_window: int = 0
    uptime_percent: float | None = None


class PublicTunnelConnectionEvent(BaseModel):
    id: UUID
    occurred_at: datetime
    event_type: str
    outcome: str | None = None
    message: str | None = None


class PublicTunnelMetrics(BaseModel):
    project_slug: str
    service_id: UUID
    service_name: str
    service_slug: str
    component_kind: str
    range_start: datetime
    range_end: datetime
    hours: int
    latest: PublicTunnelLatestDiagnostics | None = None
    points: list[PublicTunnelMetricPoint]
    ping_samples: list[PublicTunnelPingSample] = []
    events: list[PublicTunnelConnectionEvent] = []
