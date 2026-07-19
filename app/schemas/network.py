from pydantic import BaseModel, Field


class VpnCheckConfig(BaseModel):
    config_text: str = Field(min_length=10, max_length=200_000)


class NetworkSummary(BaseModel):
    interface: str | None = None
    ipv4_address: str | None = None
    gateway: str | None = None
    dns_servers: list[str] | None = None
    mtu: int | None = None
    connect_time_ms: int | None = None
    proxy_url: str | None = None
    inbound_protocol: str | None = None
    probe_url: str | None = None
    exit_ip: str | None = None
    probe_latency_ms: int | None = None
    gateway_ping_avg_ms: float | None = None
    gateway_ping_loss_percent: float | None = None
    gateway_ping_jitter_ms: float | None = None
    download_mbps: float | None = None
    download_bytes: int | None = None
    download_duration_ms: int | None = None
    speed_test_ok: bool | None = None
    speed_test_error: str | None = None
    speed_test_measured_at: str | None = None
    speed_test_last_success_at: str | None = None
    speed_test_showing_last_success: bool | None = None
