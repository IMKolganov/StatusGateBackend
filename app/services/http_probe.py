"""HTTP reachability probes (host or VPN netns)."""

from __future__ import annotations

import subprocess
import time
from typing import Any

import httpx

from app.core.probe_defaults import DEFAULT_PROBE_URL, GOOGLE_PROBE_URL

__all__ = ["DEFAULT_PROBE_URL", "GOOGLE_PROBE_URL", "_probe_endpoint", "_probe_endpoint_via_curl"]

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


