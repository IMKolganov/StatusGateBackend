"""Persistent OpenVPN session lifecycle and probes."""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.schemas.monitored_component import DEFAULT_SPEED_TEST_BYTES
from app.services.host_wan_speed import attach_host_wan_baseline_to_network
from app.services.http_probe import default_probe_url, google_probe_url, _probe_endpoint
from app.services.network_enrich import (
    _apply_cached_speed_test,
    _apply_cached_speed_test_upload,
    _collect_network_details,
    _enrich_network_metrics,
)
from app.services.speed_test_config import SpeedTestRunContext
from app.services.tun_iface import (
    _interface_is_up,
    _resolve_tun_gateway,
    _tun_ipv4_addresses,
    _wait_for_tun_interface,
)
from app.services.vpn_netns import (
    delete_netns,
    ensure_netns,
    move_iface_to_netns,
    netns_name_for_component,
    run_ip_command,
    tun_name_for_component,
)
from app.services.vpn_process_utils import _read_tail, _terminate_process, _vpn_log_hint


def _speed_test_bytes_for(component: MonitoredComponent) -> int:
    return component.speed_test_bytes or DEFAULT_SPEED_TEST_BYTES


def _config_text(component: MonitoredComponent) -> str:
    if not component.check_config:
        raise ValueError("Missing VPN config")
    config_text = component.check_config.get("config_text")
    if not isinstance(config_text, str) or not config_text.strip():
        raise ValueError("VPN config_text is required")
    return config_text.strip()


@dataclass
class OpenVpnSessionHandle:
    component_id: UUID
    netns: str
    proc: subprocess.Popen[Any]
    iface: str
    tmpdir: str
    config_path: Path
    log_path: Path
    pid_path: Path
    connect_time_ms: int


@dataclass
class OpenVpnStartResult:
    handle: OpenVpnSessionHandle | None
    log_tail: str | None = None
    error_message: str | None = None


def start_openvpn_persistent_session(component: MonitoredComponent) -> OpenVpnStartResult:
    config_text = _config_text(component)
    netns = netns_name_for_component(component.id)
    tun_dev = tun_name_for_component(component.id)
    ensure_netns(netns)

    tmpdir = tempfile.mkdtemp(prefix="sg-openvpn-persist-")
    config_path = Path(tmpdir) / "client.ovpn"
    log_path = Path(tmpdir) / "openvpn.log"
    pid_path = Path(tmpdir) / "openvpn.pid"
    config_path.write_text(config_text, encoding="utf-8")

    connect_started = time.perf_counter()
    # Connect in the container/default netns (has uplink to the VPN server).
    # `--route-noexec` keeps OpenVPN from hijacking the container default route
    # while multiple persistent tunnels run. After the TUN is up we move it into
    # the per-component netns and install a default route there for probes.
    proc = subprocess.Popen(
        [
            "openvpn",
            "--config",
            str(config_path),
            "--dev",
            tun_dev,
            "--route-noexec",
            "--pull-filter",
            "ignore",
            "block-outside-dns",
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

    # Persistent handshakes often need longer than short HTTP-style timeouts.
    connect_timeout = min(max(component.timeout_seconds, 60), 120)
    iface = _wait_for_tun_interface(timeout=connect_timeout, device=tun_dev)
    connect_time_ms = int((time.perf_counter() - connect_started) * 1000)
    if iface is None:
        log_tail = _read_tail(log_path)
        hint = _vpn_log_hint(log_tail)
        message = "OpenVPN tunnel did not come up in time"
        if hint:
            message = f"{message}: {hint}"
        _terminate_process(proc, pid_path)
        _cleanup_persistent_tmpdir(tmpdir)
        delete_netns(netns)
        return OpenVpnStartResult(handle=None, log_tail=log_tail, error_message=message)

    addresses = _tun_ipv4_addresses(iface)
    log_tail = _read_tail(log_path)
    gateway = _resolve_tun_gateway(iface, log_tail=log_tail)
    try:
        move_iface_to_netns(iface, netns, addresses=addresses, gateway=gateway)
    except subprocess.CalledProcessError as exc:
        log_tail = _read_tail(log_path)
        message = f"Failed to move TUN into netns {netns!r}: {exc}"
        _terminate_process(proc, pid_path)
        _cleanup_persistent_tmpdir(tmpdir)
        delete_netns(netns)
        return OpenVpnStartResult(handle=None, log_tail=log_tail, error_message=message)

    # Brief settle: address restore / link state can lag one poll.
    deadline = time.time() + 3
    while time.time() < deadline:
        if _interface_is_up(iface, netns=netns):
            break
        time.sleep(0.2)
    else:
        log_tail = _read_tail(log_path)
        message = (
            f"TUN {iface} was not up after moving into netns {netns} "
            f"(addresses={addresses!r}, gateway={gateway!r})"
        )
        _terminate_process(proc, pid_path)
        _cleanup_persistent_tmpdir(tmpdir)
        delete_netns(netns)
        return OpenVpnStartResult(handle=None, log_tail=log_tail, error_message=message)

    return OpenVpnStartResult(
        handle=OpenVpnSessionHandle(
            component_id=component.id,
            netns=netns,
            proc=proc,
            iface=iface,
            tmpdir=tmpdir,
            config_path=config_path,
            log_path=log_path,
            pid_path=pid_path,
            connect_time_ms=connect_time_ms,
        )
    )


def stop_openvpn_persistent_session(handle: OpenVpnSessionHandle) -> None:
    _terminate_process(handle.proc, handle.pid_path)
    _cleanup_persistent_tmpdir(handle.tmpdir)
    delete_netns(handle.netns)


def is_openvpn_persistent_session_up(handle: OpenVpnSessionHandle) -> bool:
    if handle.proc.poll() is not None:
        return False
    return _interface_is_up(handle.iface, netns=handle.netns)


def resolve_persistent_gateway(handle: OpenVpnSessionHandle) -> str | None:
    """In-tunnel gateway IP for the continuous pinger (first VPN hop)."""
    return _resolve_tun_gateway(handle.iface, log_tail=_read_tail(handle.log_path), netns=handle.netns)


def run_openvpn_persistent_probe(
    component: MonitoredComponent,
    handle: OpenVpnSessionHandle,
    *,
    speed_test_context: SpeedTestRunContext,
    session_event: str = "probe",
) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    probe_url = component.check_url or default_probe_url()
    timeout = component.timeout_seconds

    if not is_openvpn_persistent_session_up(handle):
        log_tail = _read_tail(handle.log_path)
        return CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=CheckOutcome.DOWN.value,
            latency_ms=int((time.perf_counter() - started) * 1000),
            http_status_code=None,
            error_message="OpenVPN tunnel is down",
            details={
                "check_type": component.check_type,
                "connection_mode": ConnectionMode.PERSISTENT.value,
                "session_event": session_event,
                "network": {"connect_time_ms": handle.connect_time_ms},
                "log_tail": log_tail,
            },
        )

    network = _collect_network_details(handle.iface, netns=handle.netns)
    network["connect_time_ms"] = handle.connect_time_ms
    network["interface"] = handle.iface

    probe = _probe_endpoint(probe_url, timeout=min(15, timeout), netns=handle.netns)
    network["probe"] = probe
    network["google_probe"] = _probe_endpoint(google_probe_url(), timeout=min(10, timeout), netns=handle.netns)

    if probe.get("ok"):
        speed_test_bytes = _speed_test_bytes_for(component)
        _enrich_network_metrics(
            network,
            gateway=network.get("gateway"),
            proxy_url=None,
            iface=handle.iface,
            timeout=min(30, max(15, timeout - handle.connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
            speed_test_bytes=speed_test_bytes,
            speed_test_context=speed_test_context,
            netns=handle.netns,
        )
    else:
        # Keep last successful speed on the public row even when the HTTP probe fails.
        _apply_cached_speed_test(network, speed_test_context)
        if (
            speed_test_context.previous_speed_test_upload
            or speed_test_context.last_successful_speed_test_upload
            or speed_test_context.previous_speed_test_upload_stats
        ):
            _apply_cached_speed_test_upload(network, speed_test_context)
        attach_host_wan_baseline_to_network(network)

    if probe.get("ok"):
        outcome = CheckOutcome.UP.value
        error_message = None
    else:
        outcome = CheckOutcome.DEGRADED.value
        error_message = probe.get("error") or "Probe through VPN failed while tunnel is up"

    log_tail = _read_tail(handle.log_path)
    details: dict[str, Any] = {
        "check_type": component.check_type,
        "connection_mode": ConnectionMode.PERSISTENT.value,
        "session_event": session_event,
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


def _cleanup_persistent_tmpdir(tmpdir: str) -> None:
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)


