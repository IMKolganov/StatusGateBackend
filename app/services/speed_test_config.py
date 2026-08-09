"""Speed-test URL templates, scheduling, and shared live slot."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.speed_test_defaults import (
    CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE,
    CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS,
    CLOUDFLARE_SPEED_TEST_ORIGIN,
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SPEED_TEST_MIN_GAP_SECONDS,
    SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS,
)
from app.models.check_result import CheckResult
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings
from app.services.speed_test_memory import (
    _is_deferred_speed_test_row,
    _is_live_speed_test_row,
    extract_last_live_speed_test_from_details,
    extract_last_successful_speed_test,
    extract_speed_test_from_details,
    extract_speed_test_stats,
    extract_speed_test_upload_from_details,
    extract_speed_test_upload_stats,
    extract_last_live_speed_test_upload_from_details,
    extract_last_successful_speed_test_upload,
    hydrate_speed_test_measured_at,
    is_meaningful_speed_test_success,
    is_rate_limited_speed_test,
    parse_speed_test_measured_at,
    pick_display_speed_test,
    resolve_speed_test_memory,
    resolve_speed_test_upload_memory,
    stamp_speed_test_measured_at,
    update_speed_test_stats,
)

# Re-export memory helpers so existing imports from speed_test_config keep working.
_speed_test_last_at: float = 0.0
_speed_test_slot_lock = threading.Lock()


@dataclass(frozen=True)
class SpeedTestRunContext:
    url_template: str
    run_speed_test: bool
    previous_speed_test: dict[str, Any] | None = None
    last_successful_speed_test: dict[str, Any] | None = None
    previous_speed_test_stats: dict[str, Any] | None = None
    # Last non-cached attempt (success or 429) — carried across deferred rows for scheduling.
    last_live_speed_test: dict[str, Any] | None = None
    # Parallel memory for VPN upload (__up) measured in the same live slot as download.
    previous_speed_test_upload: dict[str, Any] | None = None
    last_successful_speed_test_upload: dict[str, Any] | None = None
    previous_speed_test_upload_stats: dict[str, Any] | None = None
    last_live_speed_test_upload: dict[str, Any] | None = None

    @classmethod
    def default(cls) -> SpeedTestRunContext:
        return cls(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)


def reset_cloudflare_speed_test_slot_for_tests() -> None:
    """Reset the shared live speed-test slot (name kept for existing tests)."""
    global _speed_test_last_at
    with _speed_test_slot_lock:
        _speed_test_last_at = 0.0


def is_cloudflare_speed_test_template(template: str) -> bool:
    origin = CLOUDFLARE_SPEED_TEST_ORIGIN.rstrip("/")
    return template.strip().startswith(f"{origin}/")


def try_acquire_speed_test_slot(*, now: float | None = None) -> bool:
    """Allow at most one live speed test per min gap across all VPN checks in this worker."""
    global _speed_test_last_at
    current = now if now is not None else time.monotonic()
    with _speed_test_slot_lock:
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


def build_speed_test_upload_url(template: str) -> str | None:
    """Derive Cloudflare ``__up`` URL from a download template, or None if unsupported.

    Custom non-Cloudflare download templates have no known upload twin — skip upload.
    """
    trimmed = validate_speed_test_url_template(template)
    if "__down" not in trimmed:
        return None
    # Upload size is the POST body; drop the download bytes query placeholder.
    upload_template = trimmed.replace("__down", "__up")
    upload_template = upload_template.replace("?bytes={bytes}", "").replace("&bytes={bytes}", "")
    upload_template = upload_template.replace("bytes={bytes}", "")
    if upload_template.endswith("?") or upload_template.endswith("&"):
        upload_template = upload_template[:-1]
    return upload_template


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
    checked_at = latest_result.checked_at
    previous = extract_speed_test_from_details(details, checked_at=checked_at)
    if previous is None:
        return True

    # Schedule from the last REAL attempt (live row or preserved last_attempt), not from a
    # cached display row that would otherwise short-circuit the interval / 429 backoff.
    scheduling = extract_last_live_speed_test_from_details(details, checked_at=checked_at)
    if scheduling is None and _is_deferred_speed_test_row(previous):
        # Cached/deferred/throttled carrying measured_at from the last live success —
        # still honor the interval. No timestamp → never measured live → stay due.
        if parse_speed_test_measured_at(previous) is None:
            return True
        scheduling = previous
    if scheduling is None:
        scheduling = previous

    # Zero-byte "success" must not satisfy the interval — retry immediately.
    if (
        _is_live_speed_test_row(scheduling)
        and scheduling.get("ok") is True
        and not is_meaningful_speed_test_success(scheduling)
    ):
        return True

    retry_after = effective_speed_test_retry_seconds(component, settings, scheduling)
    if retry_after <= 0:
        return True

    current = now or datetime.now(UTC)
    measured_at = parse_speed_test_measured_at(scheduling)
    if measured_at is None:
        # Legacy live rows without measured_at: fall back to check time once.
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
        checked_at = latest.checked_at if latest else None
        scheduling = extract_last_live_speed_test_from_details(details, checked_at=checked_at)
        if scheduling is None:
            scheduling = extract_speed_test_from_details(details, checked_at=checked_at)
        measured_at = parse_speed_test_measured_at(scheduling) or datetime.min.replace(tzinfo=UTC)
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

    host = CLOUDFLARE_SPEED_TEST_ORIGIN.removeprefix("https://").removeprefix("http://").rstrip("/")
    return (
        f"{len(active_vpn)} active VPN services may trigger about {per_minute:.1f} speed tests per minute "
        f"on {host} from this server (Cloudflare has no published limit; HTTP 429 may occur above ~"
        f"{CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE}/min). "
        "The worker enforces at least "
        f"{CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS}s between live speed tests and staggers which VPN runs next. "
        "Use a custom speed test URL, increase speed-test intervals, or reduce polling frequency."
    )


