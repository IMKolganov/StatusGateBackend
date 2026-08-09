"""Speed-test row memory, stats, and display selection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

def _is_live_speed_test_row(speed_test: dict[str, Any]) -> bool:
    return not bool(
        speed_test.get("cached")
        or speed_test.get("deferred")
        or speed_test.get("throttled")
        or speed_test.get("stale")
    )


def _is_deferred_speed_test_row(speed_test: dict[str, Any]) -> bool:
    return bool(speed_test.get("cached") or speed_test.get("deferred") or speed_test.get("throttled"))


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


def stamp_speed_test_measured_at(speed_test: dict[str, Any], *, when: datetime | None = None) -> dict[str, Any]:
    stamped = dict(speed_test)
    stamped["measured_at"] = (when or datetime.now(UTC)).isoformat()
    return stamped


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


def extract_last_live_speed_test_upload_from_details(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    last_attempt = network.get("speed_test_upload_last_attempt")
    if isinstance(last_attempt, dict) and _is_live_speed_test_row(last_attempt):
        return hydrate_speed_test_measured_at(last_attempt, checked_at=checked_at)

    speed_test = network.get("speed_test_upload")
    if isinstance(speed_test, dict) and _is_live_speed_test_row(speed_test):
        return hydrate_speed_test_measured_at(speed_test, checked_at=checked_at)
    return None


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


def extract_speed_test_upload_from_details(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    speed_test = network.get("speed_test_upload")
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


def extract_last_successful_speed_test_upload(
    details: dict[str, Any] | None,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    last_success = network.get("speed_test_upload_last_success")
    if isinstance(last_success, dict) and is_meaningful_speed_test_success(last_success):
        return hydrate_speed_test_measured_at(last_success, checked_at=checked_at)
    speed_test = network.get("speed_test_upload")
    if isinstance(speed_test, dict) and is_meaningful_speed_test_success(speed_test):
        return hydrate_speed_test_measured_at(speed_test, checked_at=checked_at)
    return None


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


def extract_speed_test_upload_stats(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    stats = network.get("speed_test_upload_stats")
    if isinstance(stats, dict) and _normalize_speed_test_stats(stats) is not None:
        return _normalize_speed_test_stats(stats)

    last_success = extract_last_successful_speed_test_upload(details)
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


def resolve_speed_test_upload_memory(
    latest_details: dict[str, Any] | None,
    *,
    latest_checked_at: datetime | None = None,
    history_details: dict[str, Any] | None = None,
    history_checked_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Same as ``resolve_speed_test_memory`` for the VPN upload series."""
    previous = extract_speed_test_upload_from_details(latest_details, checked_at=latest_checked_at)
    last_success = extract_last_successful_speed_test_upload(
        latest_details, checked_at=latest_checked_at
    )
    stats = extract_speed_test_upload_stats(latest_details)

    if last_success is None and history_details is not None:
        last_success = extract_last_successful_speed_test_upload(
            history_details,
            checked_at=history_checked_at,
        )
    if stats is None and history_details is not None:
        stats = extract_speed_test_upload_stats(history_details)
    if stats is None and is_meaningful_speed_test_success(last_success):
        stats = extract_speed_test_upload_stats(
            {"network": {"speed_test_upload_last_success": last_success}}
        )

    return previous, last_success, stats


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


