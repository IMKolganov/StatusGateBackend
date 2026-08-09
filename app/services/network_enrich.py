"""Network detail collection and VPN speed-test enrichment."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from app.schemas.monitored_component import DEFAULT_SPEED_TEST_BYTES
from app.services import speed_measure
from app.services.host_wan_speed import attach_host_wan_baseline_to_network
from app.services.speed_test_config import (
    SpeedTestRunContext,
    build_speed_test_upload_url,
    build_speed_test_url,
    is_meaningful_speed_test_success,
    pick_display_speed_test,
    stamp_speed_test_measured_at,
    try_acquire_speed_test_slot,
    update_speed_test_stats,
)
from app.services.vpn_netns import run_ip_command

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
        if (
            speed_test_context.previous_speed_test_upload
            or speed_test_context.last_successful_speed_test_upload
            or speed_test_context.previous_speed_test_upload_stats
            or build_speed_test_upload_url(speed_test_context.url_template)
        ):
            _apply_cached_speed_test_upload(network, speed_test_context)
        attach_host_wan_baseline_to_network(network)
        return

    url = build_speed_test_url(speed_test_context.url_template, speed_test_bytes)
    upload_url = build_speed_test_upload_url(speed_test_context.url_template)
    if not try_acquire_speed_test_slot():
        _apply_cached_speed_test(network, speed_test_context, throttled=True)
        if upload_url or (
            speed_test_context.previous_speed_test_upload
            or speed_test_context.last_successful_speed_test_upload
            or speed_test_context.previous_speed_test_upload_stats
        ):
            _apply_cached_speed_test_upload(network, speed_test_context, throttled=True)
        attach_host_wan_baseline_to_network(network)
        return

    _record_live_speed_test(
        network,
        measure=speed_measure.measure_download_speed(
            url,
            proxy_url=proxy_url,
            timeout=max(5, timeout),
            netns=netns,
        ),
        url=url,
        empty_error="Speed test downloaded no data",
        result_key="speed_test",
        last_success_key="speed_test_last_success",
        stats_key="speed_test_stats",
        previous_stats=speed_test_context.previous_speed_test_stats,
        last_successful=speed_test_context.last_successful_speed_test,
        fallback_stats=speed_test_context.previous_speed_test_stats,
    )

    if upload_url:
        _record_live_speed_test(
            network,
            measure=speed_measure.measure_upload_speed(
                upload_url,
                bytes_count=speed_test_bytes,
                proxy_url=proxy_url,
                timeout=max(5, timeout),
                netns=netns,
            ),
            url=upload_url,
            empty_error="Speed test uploaded no data",
            result_key="speed_test_upload",
            last_success_key="speed_test_upload_last_success",
            stats_key="speed_test_upload_stats",
            previous_stats=speed_test_context.previous_speed_test_upload_stats,
            last_successful=speed_test_context.last_successful_speed_test_upload,
            fallback_stats=speed_test_context.previous_speed_test_upload_stats,
        )
    elif (
        speed_test_context.previous_speed_test_upload
        or speed_test_context.last_successful_speed_test_upload
        or speed_test_context.previous_speed_test_upload_stats
    ):
        _apply_cached_speed_test_upload(network, speed_test_context)

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

    attach_host_wan_baseline_to_network(network)


def _record_live_speed_test(
    network: dict[str, Any],
    *,
    measure: dict[str, Any] | None,
    url: str,
    empty_error: str,
    result_key: str,
    last_success_key: str,
    stats_key: str,
    previous_stats: dict[str, Any] | None,
    last_successful: dict[str, Any] | None,
    fallback_stats: dict[str, Any] | None,
) -> None:
    if measure:
        speed = stamp_speed_test_measured_at(measure)
        if speed.get("ok") is True and not is_meaningful_speed_test_success(speed):
            speed = {
                **speed,
                "ok": False,
                "error": empty_error,
            }
        network[result_key] = speed
        if is_meaningful_speed_test_success(speed):
            network[last_success_key] = speed
            try:
                mbps = float(speed["mbps"])
            except (KeyError, TypeError, ValueError):
                mbps = None
            if mbps is not None and mbps > 0:
                network[stats_key] = update_speed_test_stats(previous_stats, mbps=mbps)
        else:
            if last_successful:
                network[last_success_key] = last_successful
            if fallback_stats:
                network[stats_key] = fallback_stats
        return

    network[result_key] = stamp_speed_test_measured_at(
        {
            "ok": False,
            "url": url,
            "error": "Speed test failed",
        }
    )
    if last_successful:
        network[last_success_key] = last_successful
    if fallback_stats:
        network[stats_key] = fallback_stats


def _apply_cached_speed_test(
    network: dict[str, Any],
    speed_test_context: SpeedTestRunContext,
    *,
    throttled: bool = False,
) -> None:
    _apply_cached_speed_family(
        network,
        previous=speed_test_context.previous_speed_test,
        last_live=speed_test_context.last_live_speed_test,
        last_success=speed_test_context.last_successful_speed_test,
        previous_stats=speed_test_context.previous_speed_test_stats,
        result_key="speed_test",
        last_attempt_key="speed_test_last_attempt",
        last_success_key="speed_test_last_success",
        stats_key="speed_test_stats",
        empty_error="Speed test downloaded no data",
        deferred_slot_error="Speed test deferred (shared rate limit — retry on next interval)",
        deferred_stagger_error="Speed test deferred (waiting for a free slot among VPN services)",
        throttled=throttled,
    )


def _apply_cached_speed_test_upload(
    network: dict[str, Any],
    speed_test_context: SpeedTestRunContext,
    *,
    throttled: bool = False,
) -> None:
    _apply_cached_speed_family(
        network,
        previous=speed_test_context.previous_speed_test_upload,
        last_live=speed_test_context.last_live_speed_test_upload,
        last_success=speed_test_context.last_successful_speed_test_upload,
        previous_stats=speed_test_context.previous_speed_test_upload_stats,
        result_key="speed_test_upload",
        last_attempt_key="speed_test_upload_last_attempt",
        last_success_key="speed_test_upload_last_success",
        stats_key="speed_test_upload_stats",
        empty_error="Speed test uploaded no data",
        deferred_slot_error="Upload speed test deferred (shared rate limit — retry on next interval)",
        deferred_stagger_error="Upload speed test deferred (waiting for a free slot among VPN services)",
        throttled=throttled,
    )


def _apply_cached_speed_family(
    network: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    last_live: dict[str, Any] | None,
    last_success: dict[str, Any] | None,
    previous_stats: dict[str, Any] | None,
    result_key: str,
    last_attempt_key: str,
    last_success_key: str,
    stats_key: str,
    empty_error: str,
    deferred_slot_error: str,
    deferred_stagger_error: str,
    throttled: bool,
) -> None:
    live = last_live
    if live is None and isinstance(previous, dict) and not (
        previous.get("cached") or previous.get("deferred") or previous.get("throttled") or previous.get("stale")
    ):
        live = previous
    if isinstance(live, dict):
        network[last_attempt_key] = {
            key: value
            for key, value in live.items()
            if key not in {"cached", "deferred", "throttled", "defer_reason", "stale"}
        }

    displayed = pick_display_speed_test(previous, last_success)
    defer_reason = "slot" if throttled else "stagger"
    if displayed:
        cached = dict(displayed)
        cached["cached"] = True
        cached["deferred"] = True
        cached["defer_reason"] = defer_reason
        if throttled:
            cached["throttled"] = True
        if not isinstance(cached.get("measured_at"), str) and isinstance(last_success, dict):
            success_measured_at = last_success.get("measured_at")
            if isinstance(success_measured_at, str) and success_measured_at.strip():
                cached["measured_at"] = success_measured_at
        if cached.get("ok") is True and not is_meaningful_speed_test_success(cached):
            cached["ok"] = False
            cached.setdefault("error", empty_error)
        network[result_key] = cached
        if last_success:
            network[last_success_key] = last_success
        elif cached.get("ok") is True and is_meaningful_speed_test_success(cached):
            network[last_success_key] = {
                key: value
                for key, value in cached.items()
                if key not in {"cached", "deferred", "throttled", "defer_reason", "stale"}
            }
        if previous_stats:
            network[stats_key] = previous_stats
        return

    network[result_key] = {
        "ok": False,
        "error": deferred_slot_error if throttled else deferred_stagger_error,
        "deferred": True,
        "defer_reason": defer_reason,
    }
    if throttled:
        network[result_key]["throttled"] = True
    if last_success:
        network[last_success_key] = last_success
    if previous_stats:
        network[stats_key] = previous_stats


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


