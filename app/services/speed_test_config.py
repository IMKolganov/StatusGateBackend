from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.speed_test_defaults import (
    CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE,
    CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS,
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SPEED_TEST_MIN_GAP_SECONDS,
    SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS,
)
from app.models.check_result import CheckResult
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings

_speed_test_last_at: float = 0.0


@dataclass(frozen=True)
class SpeedTestRunContext:
    url_template: str
    run_speed_test: bool
    previous_speed_test: dict[str, Any] | None = None
    last_successful_speed_test: dict[str, Any] | None = None

    @classmethod
    def default(cls) -> SpeedTestRunContext:
        return cls(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)


def reset_cloudflare_speed_test_slot_for_tests() -> None:
    """Reset the shared live speed-test slot (name kept for existing tests)."""
    global _speed_test_last_at
    _speed_test_last_at = 0.0


def is_cloudflare_speed_test_template(template: str) -> bool:
    return template.strip().startswith("https://speed.cloudflare.com/")


def try_acquire_speed_test_slot(*, now: float | None = None) -> bool:
    """Allow at most one live speed test per min gap across all VPN checks in this worker."""
    global _speed_test_last_at
    current = now if now is not None else time.monotonic()
    if current - _speed_test_last_at < SPEED_TEST_MIN_GAP_SECONDS:
        return False
    _speed_test_last_at = current
    return True


def try_acquire_cloudflare_speed_test_slot(*, now: float | None = None) -> bool:
    """Back-compat alias — slot now applies to every speed-test URL."""
    return try_acquire_speed_test_slot(now=now)


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
    return is_cloudflare_speed_test_template(template)


def extract_speed_test_from_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    speed_test = network.get("speed_test")
    return speed_test if isinstance(speed_test, dict) else None


def extract_last_successful_speed_test(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    last_success = network.get("speed_test_last_success")
    if isinstance(last_success, dict) and last_success.get("ok") is True:
        return last_success
    speed_test = network.get("speed_test")
    if isinstance(speed_test, dict) and speed_test.get("ok") is True:
        return speed_test
    return None


def parse_speed_test_measured_at(speed_test: dict[str, Any] | None) -> datetime | None:
    if not speed_test:
        return None
    raw = speed_test.get("measured_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def stamp_speed_test_measured_at(speed_test: dict[str, Any], *, when: datetime | None = None) -> dict[str, Any]:
    stamped = dict(speed_test)
    stamped["measured_at"] = (when or datetime.now(UTC)).isoformat()
    return stamped


def is_rate_limited_speed_test(speed_test: dict[str, Any] | None) -> bool:
    if not speed_test or speed_test.get("ok") is True:
        return False
    error = str(speed_test.get("error", ""))
    return "429" in error or "rate limit" in error.lower()


def pick_display_speed_test(
    previous_speed_test: dict[str, Any] | None,
    last_successful_speed_test: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if previous_speed_test and previous_speed_test.get("ok") is True:
        return previous_speed_test
    if last_successful_speed_test and last_successful_speed_test.get("ok") is True:
        displayed = dict(last_successful_speed_test)
        displayed["stale"] = True
        return displayed
    return previous_speed_test


def effective_speed_test_retry_seconds(
    component: MonitoredComponent,
    settings: MonitoringSettings,
    previous_speed_test: dict[str, Any] | None,
) -> int:
    interval = effective_speed_test_interval_seconds(component, settings)
    if is_rate_limited_speed_test(previous_speed_test):
        return max(interval, SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS)
    return interval


def should_run_speed_test(
    component: MonitoredComponent,
    settings: MonitoringSettings,
    latest_result: CheckResult | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not component.speed_test_enabled:
        return False

    if latest_result is None:
        return True

    details = latest_result.details if isinstance(latest_result.details, dict) else None
    previous = extract_speed_test_from_details(details)
    if previous is None:
        return True

    # Cached/throttled rows are not a fresh live attempt — keep trying until we measure.
    if previous.get("cached") or previous.get("deferred") or previous.get("throttled"):
        return True

    retry_after = effective_speed_test_retry_seconds(component, settings, previous)
    if retry_after <= 0:
        return True

    current = now or datetime.now(UTC)
    measured_at = parse_speed_test_measured_at(previous)
    if measured_at is None:
        # Legacy rows without measured_at: fall back to check time once.
        checked_at = latest_result.checked_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        measured_at = checked_at

    return (current - measured_at).total_seconds() >= retry_after


def speed_test_stagger_key(component_id: UUID, *, now: datetime) -> tuple[int, str]:
    """Hourly rotating order so the same VPN is not always first when many are due."""
    bucket = now.astimezone(UTC).strftime("%Y%m%d%H")
    digest = hashlib.sha256(f"{component_id}:{bucket}".encode()).hexdigest()
    return (int(digest[:8], 16), str(component_id))


def pick_staggered_speed_test_component_ids(
    components: list[MonitoredComponent],
    settings: MonitoringSettings,
    latest_by_id: dict[UUID, CheckResult],
    *,
    now: datetime | None = None,
    limit: int = 1,
) -> set[UUID]:
    """Among VPNs due for a live speed test, allow only `limit` (staggered / rotating)."""
    current = now or datetime.now(UTC)
    due: list[tuple[datetime, tuple[int, str], UUID]] = []
    for component in components:
        latest = latest_by_id.get(component.id)
        if not should_run_speed_test(component, settings, latest, now=current):
            continue
        details = latest.details if latest and isinstance(latest.details, dict) else None
        previous = extract_speed_test_from_details(details)
        measured_at = parse_speed_test_measured_at(previous) or datetime.min.replace(tzinfo=UTC)
        due.append((measured_at, speed_test_stagger_key(component.id, now=current), component.id))
    due.sort(key=lambda item: (item[0], item[1]))
    return {component_id for _, _, component_id in due[: max(limit, 0)]}


def effective_poll_interval_seconds(component: MonitoredComponent, settings: MonitoringSettings) -> int:
    return component.poll_interval_seconds or settings.default_poll_interval_seconds


def estimate_speed_tests_per_minute(
    components: list[MonitoredComponent],
    settings: MonitoringSettings,
) -> float:
    total = 0.0
    unbounded_due_per_cycle = 0
    for component in components:
        if not component.is_active or not component.speed_test_enabled:
            continue
        poll_interval = max(effective_poll_interval_seconds(component, settings), 1)
        speed_interval = effective_speed_test_interval_seconds(component, settings)
        if speed_interval <= 0:
            unbounded_due_per_cycle += 1
            continue
        interval = max(poll_interval, speed_interval)
        total += 60.0 / interval
    if unbounded_due_per_cycle:
        cycles_per_minute = 60.0 / max(
            effective_poll_interval_seconds(components[0], settings),
            SPEED_TEST_MIN_GAP_SECONDS,
        )
        # Worker allows one live speed test per min gap across all VPN services.
        total += min(unbounded_due_per_cycle, 1) * cycles_per_minute
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
        "The worker enforces at least "
        f"{CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS}s between live speed tests and staggers which VPN runs next. "
        "Use a custom speed test URL, increase speed-test intervals, or reduce polling frequency."
    )
