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
from app.services.xray_config import parse_xray_config_text

_vpn_check_lock = threading.Lock()

DEFAULT_PROBE_URL = "https://ifconfig.me/ip"
DEFAULT_SPEED_TEST_BYTES = 524_288
DEFAULT_SPEED_TEST_URL = f"https://speed.cloudflare.com/__down?bytes={DEFAULT_SPEED_TEST_BYTES}"


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
                _enrich_network_metrics(
                    network,
                    gateway=network.get("gateway"),
                    proxy_url=None,
                    iface=iface,
                    timeout=min(12, max(5, timeout - connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
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


def _run_xray_check(component: MonitoredComponent) -> CheckResult:
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

            network = {
                "connect_time_ms": connect_time_ms,
                "proxy_url": _mask_proxy(proxy_url),
                "inbound_protocol": _xray_inbound_protocol(config),
            }

            probe = _probe_endpoint(probe_url, timeout=min(15, timeout), proxy_url=proxy_url)
            network["probe"] = probe

            if probe.get("ok"):
                _enrich_network_metrics(
                    network,
                    gateway=None,
                    proxy_url=proxy_url,
                    iface=None,
                    timeout=min(12, max(5, timeout - connect_time_ms / 1000 - (probe.get("latency_ms") or 0) / 1000)),
                )

            outcome = CheckOutcome.UP.value if probe.get("ok") else CheckOutcome.DOWN.value
            error_message = None if probe.get("ok") else probe.get("error") or "Probe through Xray proxy failed"
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

    try:
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
) -> None:
    if gateway:
        ping = _ping_host(gateway, count=4, timeout=min(5, timeout / 2))
        if ping:
            ping["host"] = gateway
            network["gateway_ping"] = ping

    speed = _measure_download_speed(
        DEFAULT_SPEED_TEST_URL,
        proxy_url=proxy_url,
        timeout=min(10, timeout),
    )
    if speed:
        network["speed_test"] = speed

    if iface and not network.get("mtu"):
        try:
            link_output = subprocess.check_output(["ip", "-json", "link", "show", "dev", iface], text=True, timeout=5)
            link_rows = json.loads(link_output)
            if link_rows:
                network["mtu"] = link_rows[0].get("mtu")
        except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
            pass


def _ping_host(host: str, *, count: int = 4, timeout: float = 5) -> dict[str, Any] | None:
    try:
        output = subprocess.check_output(
            ["ping", "-c", str(count), "-W", "1", host],
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


def _measure_download_speed(url: str, *, proxy_url: str | None, timeout: float) -> dict[str, Any] | None:
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
            "error": str(exc),
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


def public_network_summary(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None

    probe = network.get("probe") if isinstance(network.get("probe"), dict) else {}
    gateway_ping = network.get("gateway_ping") if isinstance(network.get("gateway_ping"), dict) else {}
    speed_test = network.get("speed_test") if isinstance(network.get("speed_test"), dict) else {}
    summary = {
        "interface": network.get("interface"),
        "ipv4_address": network.get("ipv4_address"),
        "gateway": network.get("gateway"),
        "dns_servers": network.get("dns_servers"),
        "mtu": network.get("mtu"),
        "connect_time_ms": network.get("connect_time_ms"),
        "proxy_url": network.get("proxy_url"),
        "inbound_protocol": network.get("inbound_protocol"),
        "probe_url": probe.get("url"),
        "exit_ip": probe.get("exit_ip"),
        "probe_latency_ms": probe.get("latency_ms"),
        "gateway_ping_avg_ms": gateway_ping.get("avg_ms"),
        "gateway_ping_loss_percent": gateway_ping.get("loss_percent"),
        "gateway_ping_jitter_ms": gateway_ping.get("jitter_ms"),
        "download_mbps": speed_test.get("mbps"),
        "download_bytes": speed_test.get("bytes"),
        "download_duration_ms": speed_test.get("duration_ms"),
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}
