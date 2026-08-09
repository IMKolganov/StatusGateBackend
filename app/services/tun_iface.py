"""TUN interface discovery and gateway helpers for OpenVPN checks."""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from app.services.vpn_netns import run_ip_command

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


