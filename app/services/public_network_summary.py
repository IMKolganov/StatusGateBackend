"""Project VPN check details.network into public NetworkSummary DTOs."""

from __future__ import annotations

from typing import Any

from app.schemas.network import NetworkSummary
from app.services.speed_measure import format_speed_test_error
from app.services.speed_test_config import (
    extract_speed_test_stats,
    extract_speed_test_upload_stats,
    is_meaningful_speed_test_success,
)

def _project_speed_series(
    speed_test: dict[str, Any],
    last_success: dict[str, Any],
    *,
    empty_error: str,
) -> dict[str, Any]:
    """Normalize a speed_test (+ last_success) dict into display fields."""
    speed_test_ok: bool | None = None
    speed_test_error: str | None = None
    mbps = speed_test.get("mbps")
    bytes_count = speed_test.get("bytes")
    duration_ms = speed_test.get("duration_ms")
    showing_last_success = False
    measured_at = speed_test.get("measured_at") if isinstance(speed_test.get("measured_at"), str) else None
    last_success_at = (
        last_success.get("measured_at") if isinstance(last_success.get("measured_at"), str) else None
    )

    if speed_test:
        if is_meaningful_speed_test_success(speed_test):
            speed_test_ok = True
        elif speed_test.get("ok") is True and not is_meaningful_speed_test_success(speed_test):
            speed_test_ok = False
            speed_test_error = format_speed_test_error(speed_test.get("error") or empty_error)
            mbps = None
            bytes_count = speed_test.get("bytes")
            duration_ms = speed_test.get("duration_ms")
            if is_meaningful_speed_test_success(last_success):
                mbps = last_success.get("mbps")
                bytes_count = last_success.get("bytes")
                duration_ms = last_success.get("duration_ms")
                speed_test_ok = True
                showing_last_success = True
                if isinstance(last_success.get("measured_at"), str):
                    last_success_at = last_success.get("measured_at")
        elif speed_test.get("ok") is False:
            speed_test_ok = False
            raw_error = speed_test.get("error")
            speed_test_error = format_speed_test_error(raw_error) if raw_error else "Speed test failed"
            if is_meaningful_speed_test_success(last_success):
                mbps = last_success.get("mbps")
                bytes_count = last_success.get("bytes")
                duration_ms = last_success.get("duration_ms")
                speed_test_ok = True
                showing_last_success = True
                if not last_success_at and isinstance(last_success.get("measured_at"), str):
                    last_success_at = last_success.get("measured_at")
        elif speed_test.get("stale") and is_meaningful_speed_test_success({**speed_test, "ok": True}):
            speed_test_ok = True
            showing_last_success = True
            mbps = speed_test.get("mbps")
            bytes_count = speed_test.get("bytes")
            duration_ms = speed_test.get("duration_ms")
            if not last_success_at and isinstance(speed_test.get("measured_at"), str):
                last_success_at = speed_test.get("measured_at")
        if is_meaningful_speed_test_success(speed_test) and speed_test.get("cached"):
            showing_last_success = showing_last_success or bool(
                speed_test.get("stale") or speed_test.get("throttled") or speed_test.get("deferred")
            )
            if not last_success_at and isinstance(measured_at, str):
                last_success_at = measured_at

    if showing_last_success and not last_success_at and isinstance(measured_at, str):
        last_success_at = measured_at
    if (
        is_meaningful_speed_test_success(speed_test)
        and not last_success_at
        and isinstance(measured_at, str)
        and not speed_test.get("cached")
    ):
        last_success_at = measured_at

    return {
        "ok": speed_test_ok,
        "error": speed_test_error,
        "mbps": mbps,
        "bytes": bytes_count,
        "duration_ms": duration_ms,
        "showing_last_success": showing_last_success,
        "measured_at": measured_at if isinstance(measured_at, str) else None,
        "last_success_at": last_success_at if isinstance(last_success_at, str) else None,
    }


def _project_direct_speed(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "mbps": None,
            "bytes": None,
            "duration_ms": None,
            "cached": None,
            "measured_at": None,
        }
    mbps = None
    if is_meaningful_speed_test_success(payload):
        try:
            value = float(payload["mbps"])
            mbps = value if value > 0 else None
        except (KeyError, TypeError, ValueError):
            mbps = None
    measured_at = payload.get("measured_at") if isinstance(payload.get("measured_at"), str) else None
    return {
        "mbps": mbps,
        "bytes": payload.get("bytes") if mbps is not None else None,
        "duration_ms": payload.get("duration_ms") if mbps is not None else None,
        "cached": bool(payload.get("cached") or payload.get("deferred") or payload.get("stale"))
        if mbps is not None
        else None,
        "measured_at": measured_at,
    }


def public_network_summary(details: dict[str, Any] | None) -> NetworkSummary | None:
    if not details:
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    probe_value = network.get("probe")
    probe: dict[str, Any] = probe_value if isinstance(probe_value, dict) else {}
    google_probe_value = network.get("google_probe")
    google_probe: dict[str, Any] = google_probe_value if isinstance(google_probe_value, dict) else {}
    gateway_ping_value = network.get("gateway_ping")
    gateway_ping: dict[str, Any] = gateway_ping_value if isinstance(gateway_ping_value, dict) else {}

    speed_test_value = network.get("speed_test")
    speed_test: dict[str, Any] = speed_test_value if isinstance(speed_test_value, dict) else {}
    last_success_value = network.get("speed_test_last_success")
    last_success: dict[str, Any] = last_success_value if isinstance(last_success_value, dict) else {}
    download = _project_speed_series(
        speed_test, last_success, empty_error="Speed test downloaded no data"
    )

    upload_value = network.get("speed_test_upload")
    upload_test: dict[str, Any] = upload_value if isinstance(upload_value, dict) else {}
    upload_last_value = network.get("speed_test_upload_last_success")
    upload_last: dict[str, Any] = upload_last_value if isinstance(upload_last_value, dict) else {}
    upload = _project_speed_series(
        upload_test, upload_last, empty_error="Speed test uploaded no data"
    )

    stats = extract_speed_test_stats({"network": network})
    upload_stats = extract_speed_test_upload_stats({"network": network})

    direct_download = _project_direct_speed(
        network.get("direct_speed_test") if isinstance(network.get("direct_speed_test"), dict) else None
    )
    direct_upload = _project_direct_speed(
        network.get("direct_speed_test_upload")
        if isinstance(network.get("direct_speed_test_upload"), dict)
        else None
    )
    skip_reason = network.get("direct_speed_test_skip_reason")
    if not isinstance(skip_reason, str):
        skip_reason = None

    summary = NetworkSummary(
        interface=network.get("interface"),
        ipv4_address=network.get("ipv4_address"),
        gateway=network.get("gateway"),
        dns_servers=network.get("dns_servers"),
        mtu=network.get("mtu"),
        connect_time_ms=network.get("connect_time_ms"),
        proxy_url=network.get("proxy_url"),
        inbound_protocol=network.get("inbound_protocol"),
        probe_url=probe.get("url"),
        exit_ip=probe.get("exit_ip"),
        probe_latency_ms=probe.get("latency_ms"),
        google_probe_ok=google_probe.get("ok") if google_probe else None,
        google_probe_latency_ms=google_probe.get("latency_ms"),
        gateway_ping_avg_ms=gateway_ping.get("avg_ms"),
        gateway_ping_loss_percent=gateway_ping.get("loss_percent"),
        gateway_ping_jitter_ms=gateway_ping.get("jitter_ms"),
        download_mbps=download["mbps"],
        download_bytes=download["bytes"],
        download_duration_ms=download["duration_ms"],
        speed_test_ok=download["ok"],
        speed_test_error=download["error"],
        speed_test_measured_at=download["measured_at"],
        speed_test_last_success_at=download["last_success_at"],
        speed_test_showing_last_success=download["showing_last_success"] or None,
        speed_test_min_mbps=stats.get("min_mbps") if stats else None,
        speed_test_max_mbps=stats.get("max_mbps") if stats else None,
        speed_test_avg_mbps=stats.get("avg_mbps") if stats else None,
        speed_test_sample_count=stats.get("sample_count") if stats else None,
        upload_mbps=upload["mbps"],
        upload_bytes=upload["bytes"],
        upload_duration_ms=upload["duration_ms"],
        upload_speed_test_ok=upload["ok"],
        upload_speed_test_error=upload["error"],
        upload_speed_test_measured_at=upload["measured_at"],
        upload_speed_test_last_success_at=upload["last_success_at"],
        upload_speed_test_showing_last_success=upload["showing_last_success"] or None,
        upload_speed_test_min_mbps=upload_stats.get("min_mbps") if upload_stats else None,
        upload_speed_test_max_mbps=upload_stats.get("max_mbps") if upload_stats else None,
        upload_speed_test_avg_mbps=upload_stats.get("avg_mbps") if upload_stats else None,
        upload_speed_test_sample_count=upload_stats.get("sample_count") if upload_stats else None,
        direct_download_mbps=direct_download["mbps"],
        direct_download_bytes=direct_download["bytes"],
        direct_download_duration_ms=direct_download["duration_ms"],
        direct_download_cached=direct_download["cached"],
        direct_download_measured_at=direct_download["measured_at"]
        or (
            network.get("direct_speed_test_measured_at")
            if isinstance(network.get("direct_speed_test_measured_at"), str)
            else None
        ),
        direct_upload_mbps=direct_upload["mbps"],
        direct_upload_bytes=direct_upload["bytes"],
        direct_upload_duration_ms=direct_upload["duration_ms"],
        direct_upload_cached=direct_upload["cached"],
        direct_upload_measured_at=direct_upload["measured_at"],
        direct_speed_test_skip_reason=skip_reason,
    )
    if summary.model_dump(exclude_none=True):
        return summary
    return None


