"""VPN health checks for OpenVPN and Xray (orchestration).

Heavy lifting lives in focused modules under ``app.services``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome, CheckType
from app.models.monitored_component import MonitoredComponent
from app.schemas.monitored_component import DEFAULT_SPEED_TEST_BYTES
from app.services.host_wan_speed import attach_host_wan_baseline_to_network, ephemeral_openvpn_host_route_guard
from app.services.http_probe import default_probe_url, google_probe_url, _probe_endpoint, _probe_endpoint_via_curl
from app.services.network_enrich import (
    _apply_cached_speed_test,
    _apply_cached_speed_test_upload,
    _collect_network_details,
    _enrich_network_metrics,
    _parse_ping_output,
    _read_dns_servers,
)
from app.services.openvpn_session import (
    OpenVpnSessionHandle,
    OpenVpnStartResult,
    is_openvpn_persistent_session_up,
    resolve_persistent_gateway,
    run_openvpn_persistent_probe,
    start_openvpn_persistent_session,
    stop_openvpn_persistent_session,
)
from app.services.public_network_summary import public_network_summary
from app.services.speed_measure import (
    format_speed_test_error as _format_speed_test_error,
    measure_download_speed as _measure_download_speed,
    measure_upload_speed as _measure_upload_speed,
)
from app.services.speed_test_config import SpeedTestRunContext
from app.services.tun_iface import (
    _gateway_from_openvpn_log,
    _interface_is_up,
    _list_tun_interfaces,
    _tun_ipv4_addresses,
    _wait_for_tun_interface,
)
from app.services.vpn_netns import ensure_netns, move_iface_to_netns
from app.services.vpn_process_utils import (
    _error_result,
    _mask_proxy,
    _read_tail,
    _terminate_process,
    _vpn_log_hint,
)
from app.services.xray_config import parse_xray_config_text

# Re-exports kept for tests that still patch/import helpers via this module.
__all__ = [
    "OpenVpnSessionHandle",
    "OpenVpnStartResult",
    "RECONNECT_DELAY_SECONDS",
    "default_probe_url",
    "google_probe_url",
    "run_vpn_health_check",
    "start_openvpn_persistent_session",
    "stop_openvpn_persistent_session",
    "is_openvpn_persistent_session_up",
    "resolve_persistent_gateway",
    "run_openvpn_persistent_probe",
    "public_network_summary",
    "ensure_netns",
    "move_iface_to_netns",
    "_probe_endpoint",
    "_probe_endpoint_via_curl",
    "_parse_ping_output",
    "_read_dns_servers",
    "_list_tun_interfaces",
    "_interface_is_up",
    "_tun_ipv4_addresses",
    "_gateway_from_openvpn_log",
    "_wait_for_tun_interface",
    "_collect_network_details",
    "_enrich_network_metrics",
    "_terminate_process",
    "_vpn_log_hint",
    "_read_tail",
    "_measure_download_speed",
    "_measure_upload_speed",
    "_format_speed_test_error",
]

_vpn_check_lock = threading.Lock()
RECONNECT_DELAY_SECONDS = 5


def _speed_test_bytes_for(component: MonitoredComponent) -> int:
    return component.speed_test_bytes or DEFAULT_SPEED_TEST_BYTES


def run_vpn_health_check(
    component: MonitoredComponent,
    *,
    speed_test_context: SpeedTestRunContext | None = None,
) -> CheckResult:
    context = speed_test_context or SpeedTestRunContext.default()
    with _vpn_check_lock:
        if component.check_type == CheckType.OPENVPN.value:
            return _run_openvpn_check(component, speed_test_context=context)
        if component.check_type == CheckType.XRAY.value:
            return _run_xray_check(component, speed_test_context=context)
        return _error_result(component, f"Unsupported VPN check type: {component.check_type}")


def _config_text(component: MonitoredComponent) -> str:
    if not component.check_config:
        raise ValueError("Missing VPN config")
    config_text = component.check_config.get("config_text")
    if not isinstance(config_text, str) or not config_text.strip():
        raise ValueError("VPN config_text is required")
    return config_text.strip()


def _run_openvpn_check(component: MonitoredComponent, *, speed_test_context: SpeedTestRunContext) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    config_text = _config_text(component)
    probe_url = component.check_url or default_probe_url()
    timeout = component.timeout_seconds

    with ephemeral_openvpn_host_route_guard():
        with tempfile.TemporaryDirectory(prefix="sg-openvpn-") as tmpdir:
            config_path = Path(tmpdir) / "client.ovpn"
            log_path = Path(tmpdir) / "openvpn.log"
            pid_path = Path(tmpdir) / "openvpn.pid"
            config_path.write_text(config_text, encoding="utf-8")

            proc = subprocess.Popen(
                [
                    "openvpn",
                    "--config",
                    str(config_path),
                    "--dev",
                    "tun",
                    "--log",
                    str(log_path),
                    "--writepid",
                    str(pid_path),
                    "--verb",
                    "3",
                    "--auth-nocache",
                    "--inactive",
                    "3600",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                connect_started = time.perf_counter()
                iface = _wait_for_tun_interface(timeout=min(timeout, 120))
                connect_time_ms = int((time.perf_counter() - connect_started) * 1000)

                if iface is None:
                    log_tail = _read_tail(log_path)
                    hint = _vpn_log_hint(log_tail)
                    error_message = "OpenVPN tunnel did not come up in time"
                    if hint:
                        error_message = f"{error_message}: {hint}"
                    return CheckResult(
                        monitored_component_id=component.id,
                        checked_at=checked_at,
                        outcome=CheckOutcome.TIMEOUT.value,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        http_status_code=None,
                        error_message=error_message,
                        details={
                            "check_type": component.check_type,
                            "network": {"connect_time_ms": connect_time_ms},
                            "log_tail": log_tail,
                        },
                    )

                network = _collect_network_details(iface)
                network["connect_time_ms"] = connect_time_ms
                network["interface"] = iface

                probe = _probe_endpoint(probe_url, timeout=min(15, timeout))
                network["probe"] = probe
                network["google_probe"] = _probe_endpoint(google_probe_url(), timeout=min(10, timeout))

                if probe.get("ok"):
                    speed_test_bytes = _speed_test_bytes_for(component)
                    _enrich_network_metrics(
                        network,
                        gateway=network.get("gateway"),
                        proxy_url=None,
                        iface=iface,
                        timeout=min(30, max(15, timeout - connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
                        speed_test_bytes=speed_test_bytes,
                        speed_test_context=speed_test_context,
                    )
                else:
                    _apply_cached_speed_test(network, speed_test_context)
                    if (
                        speed_test_context.previous_speed_test_upload
                        or speed_test_context.last_successful_speed_test_upload
                        or speed_test_context.previous_speed_test_upload_stats
                    ):
                        _apply_cached_speed_test_upload(network, speed_test_context)
                    attach_host_wan_baseline_to_network(network)

                outcome = CheckOutcome.UP.value if probe.get("ok") else CheckOutcome.DOWN.value
                error_message = None if probe.get("ok") else probe.get("error") or "Probe through VPN failed"
                log_tail = _read_tail(log_path)
                details: dict[str, Any] = {
                    "check_type": component.check_type,
                    "network": network,
                }
                if log_tail:
                    details["log_tail"] = log_tail

                return CheckResult(
                    monitored_component_id=component.id,
                    checked_at=checked_at,
                    outcome=outcome,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    http_status_code=probe.get("status_code"),
                    error_message=error_message,
                    details=details,
                )
            except Exception as exc:  # noqa: BLE001
                log_tail = _read_tail(log_path)
                message = str(exc)
                hint = _vpn_log_hint(log_tail)
                if hint:
                    message = f"{message}: {hint}"
                return _error_result(component, message, latency_ms=int((time.perf_counter() - started) * 1000), log_tail=log_tail)
            finally:
                _terminate_process(proc, pid_path)


def _run_xray_check(component: MonitoredComponent, *, speed_test_context: SpeedTestRunContext) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    config_text = _config_text(component)
    probe_url = component.check_url or default_probe_url()
    timeout = component.timeout_seconds

    try:
        config = parse_xray_config_text(config_text)
    except json.JSONDecodeError as exc:
        return _error_result(component, f"Invalid Xray config: {exc.msg}")
    except ValueError as exc:
        return _error_result(component, f"Invalid Xray config: {exc}")

    proxy_url = _xray_proxy_url(config)
    if proxy_url is None:
        return _error_result(component, "Xray config must define a socks or http inbound with port")

    with tempfile.TemporaryDirectory(prefix="sg-xray-") as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        log_path = Path(tmpdir) / "xray.log"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                ["xray", "run", "-c", str(config_path)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        try:
            connect_started = time.perf_counter()
            ready = _wait_for_proxy(proxy_url, timeout=min(timeout, 60))
            connect_time_ms = int((time.perf_counter() - connect_started) * 1000)

            if not ready:
                log_tail = _read_tail(log_path)
                hint = _vpn_log_hint(log_tail)
                error_message = "Xray proxy did not become ready in time"
                if hint:
                    error_message = f"{error_message}: {hint}"
                return CheckResult(
                    monitored_component_id=component.id,
                    checked_at=checked_at,
                    outcome=CheckOutcome.TIMEOUT.value,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    http_status_code=None,
                    error_message=error_message,
                    details={
                        "check_type": component.check_type,
                        "network": {
                            "connect_time_ms": connect_time_ms,
                            "proxy_url": _mask_proxy(proxy_url),
                        },
                        "log_tail": log_tail,
                    },
                )

            network: dict[str, Any] = {
                "connect_time_ms": connect_time_ms,
                "proxy_url": _mask_proxy(proxy_url),
                "inbound_protocol": _xray_inbound_protocol(config),
            }

            probe_result = _probe_endpoint(probe_url, timeout=min(15, timeout), proxy_url=proxy_url)
            network["probe"] = probe_result
            network["google_probe"] = _probe_endpoint(google_probe_url(), timeout=min(10, timeout), proxy_url=proxy_url)

            probe_ok = bool(probe_result.get("ok"))
            if probe_ok:
                speed_test_bytes = _speed_test_bytes_for(component)
                _enrich_network_metrics(
                    network,
                    gateway=None,
                    proxy_url=proxy_url,
                    iface=None,
                    timeout=min(30, max(15, timeout - connect_time_ms / 1000 - (probe_result.get("latency_ms") or 0) / 1000)),
                    speed_test_bytes=speed_test_bytes,
                    speed_test_context=speed_test_context,
                )
            else:
                _apply_cached_speed_test(network, speed_test_context)
                _apply_cached_speed_test_upload(network, speed_test_context)
                attach_host_wan_baseline_to_network(network)

            outcome = CheckOutcome.UP.value if probe_ok else CheckOutcome.DOWN.value
            error_message = None if probe_ok else probe_result.get("error") or "Probe through Xray proxy failed"
            log_tail = _read_tail(log_path)
            details: dict[str, Any] = {
                "check_type": component.check_type,
                "network": network,
            }
            if log_tail:
                details["log_tail"] = log_tail

            return CheckResult(
                monitored_component_id=component.id,
                checked_at=checked_at,
                outcome=outcome,
                latency_ms=int((time.perf_counter() - started) * 1000),
                http_status_code=probe_result.get("status_code"),
                error_message=error_message,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001
            log_tail = _read_tail(log_path)
            message = str(exc)
            hint = _vpn_log_hint(log_tail)
            if hint:
                message = f"{message}: {hint}"
            return _error_result(component, message, latency_ms=int((time.perf_counter() - started) * 1000), log_tail=log_tail)
        finally:
            _terminate_process(proc)


def _xray_proxy_url(config: dict[str, Any]) -> str | None:
    for inbound in config.get("inbounds") or []:
        if not isinstance(inbound, dict):
            continue
        protocol = inbound.get("protocol")
        port = inbound.get("port")
        if isinstance(port, str) and port.strip().isdigit():
            port = int(port.strip())
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            continue
        listen = inbound.get("listen") or "127.0.0.1"
        if listen in {"0.0.0.0", "::"}:
            listen = "127.0.0.1"
        if protocol == "socks":
            return f"socks5://{listen}:{port}"
        if protocol == "http":
            return f"http://{listen}:{port}"
    return None


def _xray_inbound_protocol(config: dict[str, Any]) -> str | None:
    for inbound in config.get("inbounds") or []:
        if isinstance(inbound, dict) and inbound.get("protocol"):
            return str(inbound["protocol"])
    return None


def _wait_for_proxy(proxy_url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(proxy=proxy_url, timeout=2.0) as client:
                client.get(default_probe_url())
            return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


