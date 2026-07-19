import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome, CheckType, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.schemas.monitored_component import DEFAULT_SPEED_TEST_BYTES
from app.schemas.network import NetworkSummary
from app.services.speed_test_config import (
    SpeedTestRunContext,
    build_speed_test_url,
    pick_display_speed_test,
    stamp_speed_test_measured_at,
    try_acquire_speed_test_slot,
)
from app.services.vpn_netns import (
    delete_netns,
    ensure_netns,
    move_iface_to_netns,
    netns_name_for_component,
    run_ip_command,
    tun_name_for_component,
)
from app.services.xray_config import parse_xray_config_text

_vpn_check_lock = threading.Lock()

DEFAULT_PROBE_URL = "https://ifconfig.me/ip"
RECONNECT_DELAY_SECONDS = 5


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
    probe_url = component.check_url or DEFAULT_PROBE_URL
    timeout = component.timeout_seconds

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

            if probe.get("ok"):
                speed_test_bytes = _speed_test_bytes_for(component)
                _enrich_network_metrics(
                    network,
                    gateway=network.get("gateway"),
                    proxy_url=None,
                    iface=iface,
                    timeout=min(12, max(5, timeout - connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
                    speed_test_bytes=speed_test_bytes,
                    speed_test_context=speed_test_context,
                )

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


def run_openvpn_persistent_probe(
    component: MonitoredComponent,
    handle: OpenVpnSessionHandle,
    *,
    speed_test_context: SpeedTestRunContext,
    session_event: str = "probe",
) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    probe_url = component.check_url or DEFAULT_PROBE_URL
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

    if probe.get("ok"):
        speed_test_bytes = _speed_test_bytes_for(component)
        _enrich_network_metrics(
            network,
            gateway=network.get("gateway"),
            proxy_url=None,
            iface=handle.iface,
            timeout=min(12, max(5, timeout - handle.connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
            speed_test_bytes=speed_test_bytes,
            speed_test_context=speed_test_context,
            netns=handle.netns,
        )

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


def _run_xray_check(component: MonitoredComponent, *, speed_test_context: SpeedTestRunContext) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    config_text = _config_text(component)
    probe_url = component.check_url or DEFAULT_PROBE_URL
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

            probe_ok = bool(probe_result.get("ok"))
            if probe_ok:
                speed_test_bytes = _speed_test_bytes_for(component)
                _enrich_network_metrics(
                    network,
                    gateway=None,
                    proxy_url=proxy_url,
                    iface=None,
                    timeout=min(12, max(5, timeout - connect_time_ms / 1000 - (probe_result.get("latency_ms") or 0) / 1000)),
                    speed_test_bytes=speed_test_bytes,
                    speed_test_context=speed_test_context,
                )

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


def _wait_for_tun_interface(timeout: float, *, netns: str | None = None, device: str | None = None) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for iface in _list_tun_interfaces(netns=netns):
            if device and iface != device:
                continue
            if _interface_is_up(iface, netns=netns):
                return iface
        time.sleep(0.5)
    return None


def _list_tun_interfaces(*, netns: str | None = None) -> list[str]:
    try:
        if netns:
            output = run_ip_command(["-json", "link"], netns=netns)
        else:
            output = subprocess.check_output(["ip", "-json", "link"], text=True, timeout=5)
        links = json.loads(output)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []

    interfaces: list[str] = []
    for link in links:
        name = link.get("ifname")
        if isinstance(name, str) and name.startswith("tun"):
            interfaces.append(name)
    return interfaces


def _interface_is_up(iface: str, *, netns: str | None = None) -> bool:
    try:
        if netns:
            output = run_ip_command(["-json", "addr", "show", "dev", iface], netns=netns)
        else:
            output = subprocess.check_output(["ip", "-json", "addr", "show", "dev", iface], text=True, timeout=5)
        rows = json.loads(output)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return False

    if not rows:
        return False
    flags = rows[0].get("flags") or []
    if "UP" not in flags:
        return False
    for addr_info in rows[0].get("addr_info") or []:
        if addr_info.get("family") == "inet" and addr_info.get("local"):
            return True
    return False


def _tun_ipv4_addresses(iface: str, *, netns: str | None = None) -> list[tuple[str, int]]:
    try:
        if netns:
            output = run_ip_command(["-json", "addr", "show", "dev", iface], netns=netns)
        else:
            output = subprocess.check_output(["ip", "-json", "addr", "show", "dev", iface], text=True, timeout=5)
        rows = json.loads(output)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []

    addresses: list[tuple[str, int]] = []
    for row in rows:
        for addr_info in row.get("addr_info") or []:
            if addr_info.get("family") != "inet":
                continue
            local = addr_info.get("local")
            prefixlen = addr_info.get("prefixlen")
            if isinstance(local, str) and isinstance(prefixlen, int):
                addresses.append((local, prefixlen))
    return addresses


def _tun_peer_gateway(iface: str, *, netns: str | None = None) -> str | None:
    try:
        if netns:
            output = run_ip_command(["-json", "addr", "show", "dev", iface], netns=netns)
        else:
            output = subprocess.check_output(["ip", "-json", "addr", "show", "dev", iface], text=True, timeout=5)
        rows = json.loads(output)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return None

    for row in rows:
        for addr_info in row.get("addr_info") or []:
            if addr_info.get("family") != "inet":
                continue
            peer = addr_info.get("peer")
            if isinstance(peer, str) and peer:
                return peer.split("/")[0]
    try:
        if netns:
            route_output = run_ip_command(["-json", "route", "show", "dev", iface], netns=netns)
        else:
            route_output = subprocess.check_output(
                ["ip", "-json", "route", "show", "dev", iface], text=True, timeout=5
            )
        for route in json.loads(route_output):
            gateway = route.get("gateway")
            if isinstance(gateway, str) and gateway:
                return gateway
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def _gateway_from_openvpn_log(log_tail: str | None) -> str | None:
    if not log_tail:
        return None
    # PUSH_REPLY,...route-gateway 10.51.15.1,... or "OPTIONS IMPORT" lines
    match = re.search(r"route-gateway\s+(\d+\.\d+\.\d+\.\d+)", log_tail)
    if match:
        return match.group(1)
    match = re.search(r"net_addr_v4_add:\s+(\d+\.\d+\.\d+\.\d+)/(\d+)\s+dev", log_tail)
    if match:
        # Subnet topology often uses .1 as gateway; derive from local /24+.
        local = match.group(1)
        prefix = int(match.group(2))
        if prefix >= 24:
            parts = local.split(".")
            parts[3] = "1"
            candidate = ".".join(parts)
            if candidate != local:
                return candidate
    return None


def _resolve_tun_gateway(iface: str, *, log_tail: str | None = None, netns: str | None = None) -> str | None:
    return _tun_peer_gateway(iface, netns=netns) or _gateway_from_openvpn_log(log_tail)


def _collect_network_details(iface: str, *, netns: str | None = None) -> dict[str, Any]:
    network: dict[str, Any] = {
        "ipv4_addresses": [],
        "ipv6_addresses": [],
        "routes": [],
        "dns_servers": _read_dns_servers(),
    }

    try:
        if netns:
            addr_output = run_ip_command(["-json", "addr", "show", "dev", iface], netns=netns)
        else:
            addr_output = subprocess.check_output(["ip", "-json", "addr", "show", "dev", iface], text=True, timeout=5)
        addr_rows = json.loads(addr_output)
        for row in addr_rows:
            for addr_info in row.get("addr_info") or []:
                family = addr_info.get("family")
                local = addr_info.get("local")
                if not local:
                    continue
                if family == "inet":
                    network["ipv4_addresses"].append(local)
                elif family == "inet6":
                    network["ipv6_addresses"].append(local)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        pass

    try:
        if netns:
            route_output = run_ip_command(["-json", "route", "show", "dev", iface], netns=netns)
        else:
            route_output = subprocess.check_output(["ip", "-json", "route", "show", "dev", iface], text=True, timeout=5)
        routes = json.loads(route_output)
        for route in routes[:10]:
            network["routes"].append(
                {
                    "dst": route.get("dst"),
                    "gateway": route.get("gateway"),
                    "prefsrc": route.get("prefsrc"),
                }
            )
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        pass

    if network["ipv4_addresses"]:
        network["ipv4_address"] = network["ipv4_addresses"][0]
    if network["routes"]:
        network["gateway"] = next((route.get("gateway") for route in network["routes"] if route.get("gateway")), None)

    try:
        if netns:
            link_output = run_ip_command(["-json", "link", "show", "dev", iface], netns=netns)
        else:
            link_output = subprocess.check_output(["ip", "-json", "link", "show", "dev", iface], text=True, timeout=5)
        link_rows = json.loads(link_output)
        if link_rows:
            network["mtu"] = link_rows[0].get("mtu")
            network["operstate"] = link_rows[0].get("operstate")
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        pass

    return network


def _enrich_network_metrics(
    network: dict[str, Any],
    *,
    gateway: str | None,
    proxy_url: str | None,
    iface: str | None,
    timeout: float,
    speed_test_bytes: int = DEFAULT_SPEED_TEST_BYTES,
    speed_test_context: SpeedTestRunContext,
    netns: str | None = None,
) -> None:
    if gateway:
        ping = _ping_host(gateway, count=4, timeout=min(5, timeout / 2), netns=netns)
        if ping:
            ping["host"] = gateway
            network["gateway_ping"] = ping

    if not speed_test_context.run_speed_test:
        _apply_cached_speed_test(network, speed_test_context)
        return

    url = build_speed_test_url(speed_test_context.url_template, speed_test_bytes)
    if not try_acquire_speed_test_slot():
        _apply_cached_speed_test(network, speed_test_context, throttled=True)
        return

    speed = _measure_download_speed(
        url,
        proxy_url=proxy_url,
        timeout=max(5, timeout),
        netns=netns,
    )
    if speed:
        speed = stamp_speed_test_measured_at(speed)
        network["speed_test"] = speed
        if speed.get("ok"):
            network["speed_test_last_success"] = speed
        elif speed_test_context.last_successful_speed_test:
            network["speed_test_last_success"] = speed_test_context.last_successful_speed_test

    if iface and not network.get("mtu"):
        try:
            if netns:
                link_output = run_ip_command(["-json", "link", "show", "dev", iface], netns=netns)
            else:
                link_output = subprocess.check_output(["ip", "-json", "link", "show", "dev", iface], text=True, timeout=5)
            link_rows = json.loads(link_output)
            if link_rows:
                network["mtu"] = link_rows[0].get("mtu")
        except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
            pass


def _apply_cached_speed_test(
    network: dict[str, Any],
    speed_test_context: SpeedTestRunContext,
    *,
    throttled: bool = False,
) -> None:
    displayed = pick_display_speed_test(
        speed_test_context.previous_speed_test,
        speed_test_context.last_successful_speed_test,
    )
    if displayed:
        cached = dict(displayed)
        cached["cached"] = True
        if throttled:
            cached["throttled"] = True
        network["speed_test"] = cached
        if speed_test_context.last_successful_speed_test:
            network["speed_test_last_success"] = speed_test_context.last_successful_speed_test
        return

    if throttled:
        network["speed_test"] = {
            "ok": False,
            "error": "Speed test deferred (Cloudflare throttle — retry on next interval)",
            "deferred": True,
        }


def _ping_host(host: str, *, count: int = 4, timeout: float = 5, netns: str | None = None) -> dict[str, Any] | None:
    cmd = ["ping", "-c", str(count), "-W", "1", host]
    if netns:
        cmd = ["ip", "netns", "exec", netns, *cmd]
    try:
        output = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    return _parse_ping_output(output)


def _parse_ping_output(output: str) -> dict[str, Any] | None:
    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    rtt_match = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", output)
    if not loss_match and not rtt_match:
        return None

    result: dict[str, Any] = {}
    if loss_match:
        result["loss_percent"] = float(loss_match.group(1))
    if rtt_match:
        result["min_ms"] = float(rtt_match.group(1))
        result["avg_ms"] = float(rtt_match.group(2))
        result["max_ms"] = float(rtt_match.group(3))
        result["jitter_ms"] = float(rtt_match.group(4))
    return result


def _format_speed_test_error(error: Any) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code == 429:
            return "Speed test rate limited (HTTP 429)"
        if status_code == 403:
            return "Speed test blocked (HTTP 403)"
        return f"Speed test failed (HTTP {status_code})"
    if isinstance(error, httpx.TimeoutException):
        return "Speed test timed out"
    if isinstance(error, httpx.ConnectError):
        return "Speed test connection failed"

    message = str(error).strip() if error is not None else ""
    if not message:
        return "Speed test failed"

    # Already normalized messages from an earlier formatting pass.
    if message.startswith("Speed test "):
        return message

    status_match = re.search(r"Client error '(\d{3})", message)
    if status_match:
        status_code = int(status_match.group(1))
        if status_code == 429:
            return "Speed test rate limited (HTTP 429)"
        if status_code == 403:
            return "Speed test blocked (HTTP 403)"
        return f"Speed test failed (HTTP {status_code})"

    if "429" in message or "rate limit" in message.lower():
        return "Speed test rate limited (HTTP 429)"

    if re.search(r"\btimeout\b", message, re.IGNORECASE):
        return "Speed test timed out"

    return "Speed test failed"


def _measure_download_speed(
    url: str,
    *,
    proxy_url: str | None,
    timeout: float,
    netns: str | None = None,
) -> dict[str, Any] | None:
    if netns:
        return _measure_download_speed_curl(url, timeout=timeout, netns=netns)

    started = time.perf_counter()
    bytes_read = 0
    try:
        client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        with httpx.Client(**client_kwargs) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    bytes_read += len(chunk)
        duration_ms = max(int((time.perf_counter() - started) * 1000), 1)
        megabits = (bytes_read * 8) / 1_000_000
        seconds = duration_ms / 1000
        mbps = round(megabits / seconds, 2) if seconds > 0 else None
        return {
            "ok": True,
            "url": url,
            "bytes": bytes_read,
            "duration_ms": duration_ms,
            "mbps": mbps,
        }
    except httpx.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "bytes": bytes_read,
            "duration_ms": duration_ms,
            "error": _format_speed_test_error(exc),
        }


def _read_dns_servers() -> list[str]:
    resolv_path = Path("/etc/resolv.conf")
    if not resolv_path.exists():
        return []
    servers: list[str] = []
    for line in resolv_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*nameserver\s+(\S+)", line)
        if match:
            servers.append(match.group(1))
    return servers


def _xray_proxy_url(config: dict[str, Any]) -> str | None:
    for inbound in config.get("inbounds") or []:
        if not isinstance(inbound, dict):
            continue
        protocol = inbound.get("protocol")
        port = inbound.get("port")
        if not isinstance(port, int):
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
                client.get(DEFAULT_PROBE_URL)
            return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


def _probe_endpoint(
    url: str,
    timeout: float,
    proxy_url: str | None = None,
    *,
    netns: str | None = None,
) -> dict[str, Any]:
    if netns:
        return _probe_endpoint_via_curl(url, timeout, netns=netns)

    started = time.perf_counter()
    try:
        client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        with httpx.Client(**client_kwargs) as client:
            response = client.get(url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        body = response.text.strip()
        exit_ip = body.splitlines()[0][:64] if body else None
        return {
            "ok": 200 <= response.status_code < 400,
            "url": url,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "exit_ip": exit_ip,
            "body_preview": body[:200] if body else None,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }


def _probe_endpoint_via_curl(url: str, timeout: float, *, netns: str) -> dict[str, Any]:
    started = time.perf_counter()
    cmd = [
        "ip",
        "netns",
        "exec",
        netns,
        "curl",
        "-sS",
        "-L",
        "--max-time",
        str(max(1, int(timeout))),
        "-w",
        "\n__HTTP_CODE__:%{http_code}",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2, check=False)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "curl probe failed").strip()
            return {"ok": False, "url": url, "error": error, "latency_ms": latency_ms}

        body, _, status_part = result.stdout.rpartition("\n__HTTP_CODE__:")
        status_code = int(status_part.split(":", 1)[-1]) if status_part else None
        body = body.strip()
        exit_ip = body.splitlines()[0][:64] if body else None
        return {
            "ok": status_code is not None and 200 <= status_code < 400,
            "url": url,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "exit_ip": exit_ip,
            "body_preview": body[:200] if body else None,
        }
    except (subprocess.SubprocessError, ValueError) as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }


def _measure_download_speed_curl(url: str, *, timeout: float, netns: str) -> dict[str, Any] | None:
    started = time.perf_counter()
    cmd = [
        "ip",
        "netns",
        "exec",
        netns,
        "curl",
        "-sS",
        "-L",
        "--max-time",
        str(max(1, int(timeout))),
        "-o",
        "/dev/null",
        "-w",
        "%{size_download}",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2, check=False)
        duration_ms = max(int((time.perf_counter() - started) * 1000), 1)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "curl speed test failed").strip()
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "error": error,
            }
        bytes_read = int(float(result.stdout.strip() or 0))
        megabits = (bytes_read * 8) / 1_000_000
        seconds = duration_ms / 1000
        mbps = round(megabits / seconds, 2) if seconds > 0 else None
        return {
            "ok": True,
            "url": url,
            "bytes": bytes_read,
            "duration_ms": duration_ms,
            "mbps": mbps,
        }
    except (subprocess.SubprocessError, ValueError) as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "bytes": 0,
            "duration_ms": duration_ms,
            "error": str(exc),
        }


def _terminate_process(proc: subprocess.Popen[Any], pid_path: Path | None = None) -> None:
    if pid_path and pid_path.exists():
        try:
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()


def _vpn_log_hint(log_tail: str | None) -> str | None:
    if not log_tail:
        return None

    for line in reversed(log_tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if "AUTH_FAILED" in upper:
            return "Authentication failed (AUTH_FAILED)"
        if "TLS ERROR" in upper:
            return stripped
        if "CANNOT RESOLVE" in upper:
            return stripped
        if "CONNECTION REFUSED" in upper:
            return stripped
        if "INACTIVITY TIMEOUT" in upper:
            return stripped
        if "VERIFY ERROR" in upper or "CERTIFICATE VERIFY FAILED" in upper:
            return stripped
        if any(marker in upper for marker in (" ERROR", " FATAL", " EXITING")):
            return stripped
    return None


def _read_tail(path: Path, max_chars: int = 4000) -> str | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content:
        return None
    return content[-max_chars:]


def _mask_proxy(proxy_url: str) -> str:
    return re.sub(r"://([^:@/]+):([^@/]+)@", "://***:***@", proxy_url)


def _error_result(
    component: MonitoredComponent,
    message: str,
    *,
    latency_ms: int | None = None,
    log_tail: str | None = None,
) -> CheckResult:
    details: dict[str, Any] = {"check_type": component.check_type}
    if log_tail:
        details["log_tail"] = log_tail
    return CheckResult(
        monitored_component_id=component.id,
        checked_at=datetime.now(UTC),
        outcome=CheckOutcome.ERROR.value,
        latency_ms=latency_ms,
        http_status_code=None,
        error_message=message,
        details=details,
    )


def public_network_summary(details: dict[str, Any] | None) -> NetworkSummary | None:
    if not details:
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    probe_value = network.get("probe")
    probe: dict[str, Any] = probe_value if isinstance(probe_value, dict) else {}
    gateway_ping_value = network.get("gateway_ping")
    gateway_ping: dict[str, Any] = gateway_ping_value if isinstance(gateway_ping_value, dict) else {}
    speed_test_value = network.get("speed_test")
    speed_test: dict[str, Any] = speed_test_value if isinstance(speed_test_value, dict) else {}
    last_success_value = network.get("speed_test_last_success")
    last_success: dict[str, Any] = last_success_value if isinstance(last_success_value, dict) else {}
    speed_test_ok: bool | None = None
    speed_test_error: str | None = None
    download_mbps = speed_test.get("mbps")
    download_bytes = speed_test.get("bytes")
    download_duration_ms = speed_test.get("duration_ms")

    showing_last_success = False
    measured_at = speed_test.get("measured_at") if isinstance(speed_test.get("measured_at"), str) else None
    last_success_at = (
        last_success.get("measured_at") if isinstance(last_success.get("measured_at"), str) else None
    )

    if speed_test:
        if speed_test.get("ok") is True:
            speed_test_ok = True
        elif speed_test.get("ok") is False:
            speed_test_ok = False
            raw_error = speed_test.get("error")
            speed_test_error = _format_speed_test_error(raw_error) if raw_error else "Speed test failed"
            if last_success.get("mbps") is not None:
                download_mbps = last_success.get("mbps")
                download_bytes = last_success.get("bytes")
                download_duration_ms = last_success.get("duration_ms")
                speed_test_ok = True
                showing_last_success = True
                if not last_success_at and isinstance(last_success.get("measured_at"), str):
                    last_success_at = last_success.get("measured_at")
        elif speed_test.get("stale") and speed_test.get("mbps") is not None:
            speed_test_ok = True
            showing_last_success = True
            download_mbps = speed_test.get("mbps")
            download_bytes = speed_test.get("bytes")
            download_duration_ms = speed_test.get("duration_ms")
            if not last_success_at and isinstance(speed_test.get("measured_at"), str):
                last_success_at = speed_test.get("measured_at")
        if speed_test.get("cached") and speed_test.get("ok") is True:
            showing_last_success = showing_last_success or bool(speed_test.get("stale") or speed_test.get("throttled"))

    if showing_last_success and not last_success_at and isinstance(measured_at, str):
        last_success_at = measured_at

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
        gateway_ping_avg_ms=gateway_ping.get("avg_ms"),
        gateway_ping_loss_percent=gateway_ping.get("loss_percent"),
        gateway_ping_jitter_ms=gateway_ping.get("jitter_ms"),
        download_mbps=download_mbps,
        download_bytes=download_bytes,
        download_duration_ms=download_duration_ms,
        speed_test_ok=speed_test_ok,
        speed_test_error=speed_test_error,
        speed_test_measured_at=measured_at if isinstance(measured_at, str) else None,
        speed_test_last_success_at=last_success_at if isinstance(last_success_at, str) else None,
        speed_test_showing_last_success=showing_last_success or None,
    )
    if summary.model_dump(exclude_none=True):
        return summary
    return None
