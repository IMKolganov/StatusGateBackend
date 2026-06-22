from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.models.check_result import CheckResult
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings

from app.core.speed_test_defaults import (
    CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE,
    DEFAULT_SPEED_TEST_INTERVAL_SECONDS,
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
)
@dataclass(frozen=True)
class SpeedTestRunContext:
    url_template: str
    run_speed_test: bool
    previous_speed_test: dict[str, Any] | None = None

    @classmethod
    def default(cls) -> SpeedTestRunContext:
        return cls(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)


def validate_speed_test_url_template(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("Speed test URL template is required")
    if "{bytes}" not in trimmed:
        raise ValueError("Speed test URL template must include the {bytes} placeholder")
    if not trimmed.startswith("https://"):
        raise ValueError("Speed test URL must use HTTPS")
    if len(trimmed) > 2048:
        raise ValueError("Speed test URL template is too long")
    return trimmed


def build_speed_test_url(template: str, bytes_count: int) -> str:
    return validate_speed_test_url_template(template).format(bytes=bytes_count)


def effective_speed_test_url_template(component: MonitoredComponent, settings: MonitoringSettings) -> str:
    if component.speed_test_url_template:
        return component.speed_test_url_template.strip()
    template = settings.default_speed_test_url_template or DEFAULT_SPEED_TEST_URL_TEMPLATE
    return template.strip()


def effective_speed_test_interval_seconds(component: MonitoredComponent, settings: MonitoringSettings) -> int:
    if component.speed_test_interval_seconds is not None:
        return component.speed_test_interval_seconds
    return settings.default_speed_test_interval_seconds


def uses_default_cloudflare_template(component: MonitoredComponent, settings: MonitoringSettings) -> bool:
    template = effective_speed_test_url_template(component, settings)
    return template.startswith("https://speed.cloudflare.com/")


def extract_speed_test_from_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    speed_test = network.get("speed_test")
    return speed_test if isinstance(speed_test, dict) else None


def should_run_speed_test(
    component: MonitoredComponent,
    settings: MonitoringSettings,
    latest_result: CheckResult | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not component.speed_test_enabled:
        return False

    interval = effective_speed_test_interval_seconds(component, settings)
    if interval <= 0:
        return True

    if latest_result is None:
        return True

    previous = extract_speed_test_from_details(latest_result.details if isinstance(latest_result.details, dict) else None)
    if previous is None:
        return True

    checked_at = latest_result.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return (current - checked_at).total_seconds() >= interval


def effective_poll_interval_seconds(component: MonitoredComponent, settings: MonitoringSettings) -> int:
    return component.poll_interval_seconds or settings.default_poll_interval_seconds


def estimate_speed_tests_per_minute(
    components: list[MonitoredComponent],
    settings: MonitoringSettings,
) -> float:
    total = 0.0
    for component in components:
        if not component.is_active or not component.speed_test_enabled:
            continue
        poll_interval = max(effective_poll_interval_seconds(component, settings), 1)
        speed_interval = effective_speed_test_interval_seconds(component, settings)
        if speed_interval <= 0:
            interval = poll_interval
        else:
            interval = max(poll_interval, speed_interval)
        total += 60.0 / interval
    return total


def speed_test_rate_warning(
    components: list[MonitoredComponent],
    settings: MonitoringSettings,
) -> str | None:
    active_vpn = [component for component in components if component.is_active and component.speed_test_enabled]
    if not active_vpn:
        return None

    uses_cloudflare = any(uses_default_cloudflare_template(component, settings) for component in active_vpn)
    if not uses_cloudflare:
        return None

    per_minute = estimate_speed_tests_per_minute(active_vpn, settings)
    if per_minute <= CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE:
        return None

    return (
        f"{len(active_vpn)} active VPN services may trigger about {per_minute:.1f} speed tests per minute "
        f"on speed.cloudflare.com from this server (Cloudflare has no published limit; HTTP 429 may occur above ~"
        f"{CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE}/min). "
        "Use a custom speed test URL, increase speed-test intervals, or reduce polling frequency."
    )
