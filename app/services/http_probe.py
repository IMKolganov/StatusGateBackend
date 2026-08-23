"""HTTP reachability probes (host or VPN netns)."""

from __future__ import annotations

import subprocess
import time
from typing import Any

import httpx

from app.core.probe_defaults import default_probe_url, google_probe_url
from app.models.enums import IpFamily
from app.services.http_client import httpx_client

__all__ = [
    "DEFAULT_PROBE_URL",
    "GOOGLE_PROBE_URL",
    "default_probe_url",
    "google_probe_url",
    "_probe_endpoint",
    "_probe_endpoint_via_curl",
]


def __getattr__(name: str) -> str:
    # Keep `from http_probe import DEFAULT_PROBE_URL` working for attribute re-exports
    # while allowing live Settings when accessed as http_probe.DEFAULT_PROBE_URL.
    if name == "DEFAULT_PROBE_URL":
        return default_probe_url()
    if name == "GOOGLE_PROBE_URL":
        return google_probe_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _probe_endpoint(
    url: str,
    timeout: float,
    proxy_url: str | None = None,
    *,
    netns: str | None = None,
    ip_family: str = IpFamily.AUTO.value,
) -> dict[str, Any]:
    if netns:
        return _probe_endpoint_via_curl(url, timeout, netns=netns, ip_family=ip_family)

    started = time.perf_counter()
    try:
        with httpx_client(timeout=timeout, proxy=proxy_url, ip_family=ip_family) as client:
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
            "ip_family": ip_family,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "ip_family": ip_family,
        }


def _probe_endpoint_via_curl(
    url: str,
    timeout: float,
    *,
    netns: str,
    ip_family: str = IpFamily.AUTO.value,
) -> dict[str, Any]:
    started = time.perf_counter()
    cmd = [
        "ip",
        "netns",
        "exec",
        netns,
        "curl",
        "-sS",
        "-L",
    ]
    if ip_family == IpFamily.IPV4.value:
        cmd.append("-4")
    elif ip_family == IpFamily.IPV6.value:
        cmd.append("-6")
    cmd.extend(
        [
            "--max-time",
            str(max(1, int(timeout))),
            "-w",
            "\n__HTTP_CODE__:%{http_code}",
            url,
        ]
    )
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
