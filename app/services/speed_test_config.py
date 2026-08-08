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
    previous_speed_test_stats: dict[str, Any] | None = None
    # Last non-cached attempt (success or 429) — carried across deferred rows for scheduling.
    last_live_speed_test: dict[str, Any] | None = None

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


def _is_live_speed_test_row(speed_test: dict[str, Any]) -> bool:
    return not bool(
        speed_test.get("cached")
        or speed_test.get("deferred")
        or speed_test.get("throttled")
        or speed_test.get("stale")
    )


def _is_deferred_speed_test_row(speed_test: dict[str, Any]) -> bool:
    return bool(speed_test.get("cached") or speed_test.get("deferred") or speed_test.get("throttled"))


def extract_last_live_speed_test_from_details(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Last real (non-cached) speed-test attempt used for interval / 429 backoff scheduling.

    Prefers ``network.speed_test_last_attempt`` (preserved when a later cycle writes a
    cached display row), then a live ``network.speed_test``. Cached-only rows return None
    so the caller can fall back to the cached ``measured_at`` when present.
    """
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    last_attempt = network.get("speed_test_last_attempt")
    if isinstance(last_attempt, dict) and _is_live_speed_test_row(last_attempt):
        return hydrate_speed_test_measured_at(last_attempt, checked_at=checked_at)

    speed_test = network.get("speed_test")
    if isinstance(speed_test, dict) and _is_live_speed_test_row(speed_test):
        return hydrate_speed_test_measured_at(speed_test, checked_at=checked_at)
    return None


def is_meaningful_speed_test_success(speed_test: dict[str, Any] | None) -> bool:
    """True only for a real download — zero bytes / 0 Mbps must not be cached as success."""
    if not speed_test or speed_test.get("ok") is not True:
        return False
    try:
        bytes_count = float(speed_test.get("bytes") or 0)
    except (TypeError, ValueError):
        bytes_count = 0.0
    if bytes_count <= 0:
        return False
    mbps = speed_test.get("mbps")
    if mbps is None:
        return True
    try:
        return float(mbps) > 0
    except (TypeError, ValueError):
        return False


def hydrate_speed_test_measured_at(
    speed_test: dict[str, Any] | None,
    *,
    checked_at: datetime | None,
) -> dict[str, Any] | None:
    """Backfill measured_at for legacy live rows that predate the timestamp field."""
    if not speed_test:
        return None
    if speed_test.get("measured_at") or not checked_at:
        return speed_test
    if not is_meaningful_speed_test_success(speed_test) or not _is_live_speed_test_row(speed_test):
        return speed_test
    when = checked_at if checked_at.tzinfo is not None else checked_at.replace(tzinfo=UTC)
    return stamp_speed_test_measured_at(speed_test, when=when)


def extract_speed_test_from_details(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    speed_test = network.get("speed_test")
    if not isinstance(speed_test, dict):
        return None
    return hydrate_speed_test_measured_at(speed_test, checked_at=checked_at)


def extract_last_successful_speed_test(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    last_success = network.get("speed_test_last_success")
    if isinstance(last_success, dict) and is_meaningful_speed_test_success(last_success):
        return hydrate_speed_test_measured_at(last_success, checked_at=checked_at)
    speed_test = network.get("speed_test")
    if isinstance(speed_test, dict) and is_meaningful_speed_test_success(speed_test):
        return hydrate_speed_test_measured_at(speed_test, checked_at=checked_at)
    return None


def extract_speed_test_stats(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    stats = network.get("speed_test_stats")
    if isinstance(stats, dict) and _normalize_speed_test_stats(stats) is not None:
        return _normalize_speed_test_stats(stats)

    # Seed from a single known success when stats were never recorded.
    last_success = extract_last_successful_speed_test(details)
    if not is_meaningful_speed_test_success(last_success):
        return None
    try:
        mbps = float(last_success.get("mbps"))  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    return {
        "min_mbps": mbps,
        "max_mbps": mbps,
        "avg_mbps": mbps,
        "sample_count": 1,
    }


def resolve_speed_test_memory(
    latest_details: dict[str, Any] | None,
    *,
    latest_checked_at: datetime | None = None,
    history_details: dict[str, Any] | None = None,
    history_checked_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Carry speed-test memory across down/timeout gaps that wipe network details.

    Returns ``(previous_speed_test, last_successful_speed_test, speed_test_stats)``.
    ``previous_speed_test`` always comes from the latest check (interval / deferral).
    Success + stats fall back to an older check when the latest row has none.
    """
    previous = extract_speed_test_from_details(latest_details, checked_at=latest_checked_at)
    last_success = extract_last_successful_speed_test(latest_details, checked_at=latest_checked_at)
    stats = extract_speed_test_stats(latest_details)

    if last_success is None and history_details is not None:
        last_success = extract_last_successful_speed_test(
            history_details,
            checked_at=history_checked_at,
        )
    if stats is None and history_details is not None:
        stats = extract_speed_test_stats(history_details)
    if stats is None and is_meaningful_speed_test_success(last_success):
        stats = extract_speed_test_stats({"network": {"speed_test_last_success": last_success}})

    return previous, last_success, stats


def _normalize_speed_test_stats(stats: dict[str, Any]) -> dict[str, Any] | None:
    try:
        min_mbps = float(stats["min_mbps"])
        max_mbps = float(stats["max_mbps"])
        avg_mbps = float(stats["avg_mbps"])
        sample_count = int(stats["sample_count"])
    except (KeyError, TypeError, ValueError):
        return None
    if sample_count <= 0 or min_mbps <= 0 or max_mbps <= 0 or avg_mbps <= 0:
        return None
    if min_mbps > max_mbps:
        return None
    return {
        "min_mbps": round(min_mbps, 2),
        "max_mbps": round(max_mbps, 2),
        "avg_mbps": round(avg_mbps, 2),
        "sample_count": sample_count,
    }


def update_speed_test_stats(
    previous_stats: dict[str, Any] | None,
    *,
    mbps: float,
) -> dict[str, Any]:
    value = round(float(mbps), 2)
    normalized = _normalize_speed_test_stats(previous_stats) if previous_stats else None
    if normalized is None:
        return {
            "min_mbps": value,
            "max_mbps": value,
            "avg_mbps": value,
            "sample_count": 1,
        }
    count = normalized["sample_count"] + 1
    avg = ((normalized["avg_mbps"] * normalized["sample_count"]) + value) / count
    return {
        "min_mbps": round(min(normalized["min_mbps"], value), 2),
        "max_mbps": round(max(normalized["max_mbps"], value), 2),
        "avg_mbps": round(avg, 2),
        "sample_count": count,
    }


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
    if is_meaningful_speed_test_success(previous_speed_test):
        return previous_speed_test
    if is_meaningful_speed_test_success(last_successful_speed_test):
        displayed = dict(last_successful_speed_test)  # type: ignore[arg-type]
        displayed["stale"] = True
        return displayed
    # Prefer a real prior failure over a meaningless 0 Mbps "ok" row.
    if previous_speed_test and previous_speed_test.get("ok") is False:
        return previous_speed_test
    if previous_speed_test and not is_meaningful_speed_test_success(previous_speed_test):
        failed = dict(previous_speed_test)
        failed["ok"] = False
        failed.setdefault("error", "Speed test downloaded no data")
        return failed
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

    return (
        f"{len(active_vpn)} active VPN services may trigger about {per_minute:.1f} speed tests per minute "
        f"on speed.cloudflare.com from this server (Cloudflare has no published limit; HTTP 429 may occur above ~"
        f"{CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE}/min). "
        "The worker enforces at least "
        f"{CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS}s between live speed tests and staggers which VPN runs next. "
        "Use a custom speed test URL, increase speed-test intervals, or reduce polling frequency."
    )
