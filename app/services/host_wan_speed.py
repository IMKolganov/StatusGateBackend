"""Host WAN (without VPN) download/upload baseline for dual-path diagnostics.

Ephemeral OpenVPN can hijack the host default route, so WAN tests must not run
while an ephemeral session is active. Persistent OpenVPN uses netns + route-noexec
and leaves the host path alone.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from app.core.speed_test_defaults import DEFAULT_SPEED_TEST_INTERVAL_SECONDS, DEFAULT_SPEED_TEST_URL_TEMPLATE
from app.models.monitoring_settings import MonitoringSettings
from app.services.speed_test_config import (
    build_speed_test_upload_url,
    build_speed_test_url,
    is_meaningful_speed_test_success,
    parse_speed_test_measured_at,
    stamp_speed_test_measured_at,
    try_acquire_speed_test_slot,
)

_ephemeral_lock = threading.RLock()
_ephemeral_sessions = 0

_store_lock = threading.Lock()
_latest: HostWanBaseline | None = None
_history: list[HostWanBaseline] = []
_HISTORY_LIMIT = 128
# When WAN is skipped due to ephemeral OpenVPN but a prior baseline exists, remember
# the reason so attach can surface it without wiping measured Mbps.
_pending_skip_reason: str | None = None

# Default transfer size for host WAN (same as VPN default unless overridden later).
_DEFAULT_WAN_BYTES = 524_288


@dataclass(frozen=True)
class HostWanBaseline:
    measured_at: datetime
    download: dict[str, Any] | None
    upload: dict[str, Any] | None
    skipped: bool = False
    skip_reason: str | None = None

    def as_network_fields(self, *, overlay_skip_reason: str | None = None) -> dict[str, Any]:
        """Shape stamped into CheckResult details.network for public projection."""
        payload: dict[str, Any] = {
            "direct_speed_test_measured_at": self.measured_at.isoformat(),
        }
        if self.download is not None:
            payload["direct_speed_test"] = dict(self.download)
        if self.upload is not None:
            payload["direct_speed_test_upload"] = dict(self.upload)
        skip_reason = overlay_skip_reason or (self.skip_reason if self.skipped else None)
        if self.skipped or overlay_skip_reason:
            payload["direct_speed_test_skipped"] = True
            if skip_reason:
                payload["direct_speed_test_skip_reason"] = skip_reason
        return payload


def reset_host_wan_state_for_tests() -> None:
    global _ephemeral_sessions, _latest, _history, _pending_skip_reason
    with _ephemeral_lock:
        _ephemeral_sessions = 0
    with _store_lock:
        _latest = None
        _history = []
        _pending_skip_reason = None


@contextmanager
def ephemeral_openvpn_host_route_guard() -> Iterator[None]:
    """Hold while ephemeral OpenVPN may own the host default route."""
    global _ephemeral_sessions
    with _ephemeral_lock:
        _ephemeral_sessions += 1
    try:
        yield
    finally:
        with _ephemeral_lock:
            _ephemeral_sessions = max(0, _ephemeral_sessions - 1)


def ephemeral_openvpn_active() -> bool:
    with _ephemeral_lock:
        return _ephemeral_sessions > 0


def get_latest_host_wan_baseline() -> HostWanBaseline | None:
    with _store_lock:
        return _latest


def host_wan_baseline_at_or_before(when: datetime) -> HostWanBaseline | None:
    """Nearest baseline at or before ``when`` (no future fallback)."""
    target = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    with _store_lock:
        candidates = [row for row in _history if row.measured_at <= target]
        if candidates:
            return candidates[-1]
        if _latest is not None and _latest.measured_at <= target:
            return _latest
        return None


def _store_baseline(baseline: HostWanBaseline) -> None:
    global _latest, _history, _pending_skip_reason
    with _store_lock:
        _latest = baseline
        _history.append(baseline)
        if len(_history) > _HISTORY_LIMIT:
            _history = _history[-_HISTORY_LIMIT:]
        if not baseline.skipped:
            _pending_skip_reason = None


def _should_run_host_wan(*, interval_seconds: int, now: datetime) -> bool:
    latest = get_latest_host_wan_baseline()
    if latest is None:
        return True
    # Skipped attempts should retry sooner once the ephemeral session ends.
    if latest.skipped:
        return True
    measured = latest.measured_at
    return now - measured >= timedelta(seconds=max(1, interval_seconds))


def attach_host_wan_baseline_to_network(network: dict[str, Any], *, when: datetime | None = None) -> None:
    """Copy the best host WAN snapshot onto a VPN check's network details."""
    with _store_lock:
        overlay = _pending_skip_reason
    baseline = (
        host_wan_baseline_at_or_before(when)
        if when is not None
        else get_latest_host_wan_baseline()
    )
    if baseline is None:
        return
    network.update(baseline.as_network_fields(overlay_skip_reason=overlay))


def run_host_wan_speed_if_due(
    settings: MonitoringSettings,
    *,
    now: datetime | None = None,
    bytes_count: int = _DEFAULT_WAN_BYTES,
) -> HostWanBaseline | None:
    """Measure host WAN down+up when due and no ephemeral OpenVPN is active."""
    global _pending_skip_reason
    current = now or datetime.now(UTC)
    interval = settings.default_speed_test_interval_seconds or DEFAULT_SPEED_TEST_INTERVAL_SECONDS
    if not _should_run_host_wan(interval_seconds=interval, now=current):
        return get_latest_host_wan_baseline()

    if ephemeral_openvpn_active():
        skipped = HostWanBaseline(
            measured_at=current,
            download=None,
            upload=None,
            skipped=True,
            skip_reason="ephemeral_openvpn_active",
        )
        # Do not overwrite a good previous baseline with a skip marker for display;
        # only record skip when we have never measured.
        if get_latest_host_wan_baseline() is None:
            _store_baseline(skipped)
        else:
            with _store_lock:
                _pending_skip_reason = "ephemeral_openvpn_active"
        return get_latest_host_wan_baseline()

    if not try_acquire_speed_test_slot():
        return get_latest_host_wan_baseline()

    from app.services import speed_measure

    template = (settings.default_speed_test_url_template or DEFAULT_SPEED_TEST_URL_TEMPLATE).strip()
    download_url = build_speed_test_url(template, bytes_count)
    upload_url = build_speed_test_upload_url(template)
    timeout = 30.0

    download = speed_measure.measure_download_speed(
        download_url, proxy_url=None, timeout=timeout, netns=None
    )
    if download:
        download = stamp_speed_test_measured_at(download, when=current)
        if download.get("ok") is True and not is_meaningful_speed_test_success(download):
            download = {
                **download,
                "ok": False,
                "error": "Speed test downloaded no data",
            }

    upload: dict[str, Any] | None = None
    if upload_url:
        # Small pause so we do not hammer Cloudflare immediately after download.
        time.sleep(0.05)
        upload = speed_measure.measure_upload_speed(
            upload_url,
            bytes_count=bytes_count,
            proxy_url=None,
            timeout=timeout,
            netns=None,
        )
        if upload:
            upload = stamp_speed_test_measured_at(upload, when=current)
            if upload.get("ok") is True and not is_meaningful_speed_test_success(upload):
                upload = {
                    **upload,
                    "ok": False,
                    "error": "Speed test uploaded no data",
                }

    baseline = HostWanBaseline(
        measured_at=current,
        download=download,
        upload=upload,
    )
    _store_baseline(baseline)
    return baseline


def direct_mbps_from_payload(payload: dict[str, Any] | None) -> float | None:
    if not is_meaningful_speed_test_success(payload):
        return None
    try:
        value = float(payload["mbps"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def baseline_age_seconds(baseline: HostWanBaseline | None, *, now: datetime | None = None) -> float | None:
    if baseline is None:
        return None
    current = now or datetime.now(UTC)
    measured = parse_speed_test_measured_at(baseline.download) or baseline.measured_at
    return max(0.0, (current - measured).total_seconds())
