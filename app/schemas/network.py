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
    google_probe_ok: bool | None = None
    google_probe_latency_ms: int | None = None
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
    speed_test_min_mbps: float | None = None
    speed_test_max_mbps: float | None = None
    speed_test_avg_mbps: float | None = None
    speed_test_sample_count: int | None = None
    # VPN upload (through tunnel / proxy)
    upload_mbps: float | None = None
    upload_bytes: int | None = None
    upload_duration_ms: int | None = None
    upload_speed_test_ok: bool | None = None
    upload_speed_test_error: str | None = None
    upload_speed_test_measured_at: str | None = None
    upload_speed_test_last_success_at: str | None = None
    upload_speed_test_showing_last_success: bool | None = None
    upload_speed_test_min_mbps: float | None = None
    upload_speed_test_max_mbps: float | None = None
    upload_speed_test_avg_mbps: float | None = None
    upload_speed_test_sample_count: int | None = None
    # Host WAN baseline (without VPN)
    direct_download_mbps: float | None = None
    direct_download_bytes: int | None = None
    direct_download_duration_ms: int | None = None
    direct_download_cached: bool | None = None
    direct_download_measured_at: str | None = None
    direct_upload_mbps: float | None = None
    direct_upload_bytes: int | None = None
    direct_upload_duration_ms: int | None = None
    direct_upload_cached: bool | None = None
    direct_upload_measured_at: str | None = None
    direct_speed_test_skip_reason: str | None = None