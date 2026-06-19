import json
import os
import re
import signal
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

_vpn_check_lock = threading.Lock()

DEFAULT_PROBE_URL = "https://ifconfig.me/ip"


def run_vpn_health_check(component: MonitoredComponent) -> CheckResult:
    with _vpn_check_lock:
        if component.check_type == CheckType.OPENVPN.value:
            return _run_openvpn_check(component)
        if component.check_type == CheckType.XRAY.value:
            return _run_xray_check(component)
        return _error_result(component, f"Unsupported VPN check type: {component.check_type}")


def _config_text(component: MonitoredComponent) -> str:
    if not component.check_config:
        raise ValueError("Missing VPN config")
    config_text = component.check_config.get("config_text")
    if not isinstance(config_text, str) or not config_text.strip():
        raise ValueError("VPN config_text is required")
    return config_text.strip()


def _run_openvpn_check(component: MonitoredComponent) -> CheckResult:
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
                return CheckResult(
                    monitored_component_id=component.id,
                    checked_at=checked_at,
                    outcome=CheckOutcome.TIMEOUT.value,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    http_status_code=None,
                    error_message="OpenVPN tunnel did not come up in time",
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

            outcome = CheckOutcome.UP.value if probe.get("ok") else CheckOutcome.DOWN.value
            error_message = None if probe.get("ok") else probe.get("error") or "Probe through VPN failed"

            return CheckResult(
                monitored_component_id=component.id,
                checked_at=checked_at,
                outcome=outcome,
                latency_ms=int((time.perf_counter() - started) * 1000),
                http_status_code=probe.get("status_code"),
                error_message=error_message,
                details={
                    "check_type": component.check_type,
                    "network": network,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _error_result(component, str(exc), latency_ms=int((time.perf_counter() - started) * 1000))
        finally:
            _terminate_process(proc, pid_path)


def _run_xray_check(component: MonitoredComponent) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    config_text = _config_text(component)
    probe_url = component.check_url or DEFAULT_PROBE_URL
    timeout = component.timeout_seconds

    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as exc:
        return _error_result(component, f"Invalid Xray JSON config: {exc.msg}")

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
                return CheckResult(
                    monitored_component_id=component.id,
                    checked_at=checked_at,
                    outcome=CheckOutcome.TIMEOUT.value,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    http_status_code=None,
                    error_message="Xray proxy did not become ready in time",
                    details={
                        "check_type": component.check_type,
                        "network": {
                            "connect_time_ms": connect_time_ms,
                            "proxy_url": _mask_proxy(proxy_url),
                        },
                        "log_tail": log_tail,
                    },
                )

            network = {
                "connect_time_ms": connect_time_ms,
                "proxy_url": _mask_proxy(proxy_url),
                "inbound_protocol": _xray_inbound_protocol(config),
            }

            probe = _probe_endpoint(probe_url, timeout=min(15, timeout), proxy_url=proxy_url)
            network["probe"] = probe

            outcome = CheckOutcome.UP.value if probe.get("ok") else CheckOutcome.DOWN.value
            error_message = None if probe.get("ok") else probe.get("error") or "Probe through Xray proxy failed"

            return CheckResult(
                monitored_component_id=component.id,
                checked_at=checked_at,
                outcome=outcome,
                latency_ms=int((time.perf_counter() - started) * 1000),
                http_status_code=probe.get("status_code"),
                error_message=error_message,
                details={
                    "check_type": component.check_type,
                    "network": network,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _error_result(component, str(exc), latency_ms=int((time.perf_counter() - started) * 1000))
        finally:
            _terminate_process(proc)


def _wait_for_tun_interface(timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for iface in _list_tun_interfaces():
            if _interface_is_up(iface):
                return iface
        time.sleep(0.5)
    return None


def _list_tun_interfaces() -> list[str]:
    try:
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


def _interface_is_up(iface: str) -> bool:
    try:
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


def _collect_network_details(iface: str) -> dict[str, Any]:
    network: dict[str, Any] = {
        "ipv4_addresses": [],
        "ipv6_addresses": [],
        "routes": [],
        "dns_servers": _read_dns_servers(),
    }

    try:
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

    return network


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


def _probe_endpoint(url: str, timeout: float, proxy_url: str | None = None) -> dict[str, Any]:
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


def _read_tail(path: Path, max_chars: int = 4000) -> str | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content:
        return None
    return content[-max_chars:]


def _mask_proxy(proxy_url: str) -> str:
    return re.sub(r"://([^:@/]+):([^@/]+)@", "://***:***@", proxy_url)


def _error_result(component: MonitoredComponent, message: str, *, latency_ms: int | None = None) -> CheckResult:
    return CheckResult(
        monitored_component_id=component.id,
        checked_at=datetime.now(UTC),
        outcome=CheckOutcome.ERROR.value,
        latency_ms=latency_ms,
        http_status_code=None,
        error_message=message,
        details={"check_type": component.check_type},
    )


def public_network_summary(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    probe = network.get("probe") if isinstance(network.get("probe"), dict) else {}
    summary = {
        "interface": network.get("interface"),
        "ipv4_address": network.get("ipv4_address"),
        "gateway": network.get("gateway"),
        "dns_servers": network.get("dns_servers"),
        "connect_time_ms": network.get("connect_time_ms"),
        "proxy_url": network.get("proxy_url"),
        "inbound_protocol": network.get("inbound_protocol"),
        "probe_url": probe.get("url"),
        "exit_ip": probe.get("exit_ip"),
        "probe_latency_ms": probe.get("latency_ms"),
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}
