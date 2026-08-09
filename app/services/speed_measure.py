"""HTTP/curl speed measurement (VPN path and host WAN)."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

import httpx

def format_speed_test_error(error: Any) -> str:
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


def measure_download_speed(
    url: str,
    *,
    proxy_url: str | None,
    timeout: float,
    netns: str | None = None,
) -> dict[str, Any] | None:
    if netns:
        return measure_download_speed_curl(url, timeout=timeout, netns=netns)

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
        if bytes_read <= 0:
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "mbps": 0.0,
                "error": "Speed test downloaded no data",
            }
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
            "error": format_speed_test_error(exc),
        }


def measure_upload_speed(
    url: str,
    *,
    bytes_count: int,
    proxy_url: str | None,
    timeout: float,
    netns: str | None = None,
) -> dict[str, Any] | None:
    if netns:
        return measure_upload_speed_curl(url, bytes_count=bytes_count, timeout=timeout, netns=netns)

    payload_size = max(0, int(bytes_count))
    started = time.perf_counter()
    try:
        client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        body = b"0" * payload_size
        with httpx.Client(**client_kwargs) as client:
            response = client.post(url, content=body)
            response.raise_for_status()
        duration_ms = max(int((time.perf_counter() - started) * 1000), 1)
        megabits = (payload_size * 8) / 1_000_000
        seconds = duration_ms / 1000
        mbps = round(megabits / seconds, 2) if seconds > 0 else None
        if payload_size <= 0:
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "mbps": 0.0,
                "error": "Speed test uploaded no data",
            }
        return {
            "ok": True,
            "url": url,
            "bytes": payload_size,
            "duration_ms": duration_ms,
            "mbps": mbps,
        }
    except httpx.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "bytes": 0,
            "duration_ms": duration_ms,
            "error": format_speed_test_error(exc),
        }


def measure_download_speed_curl(url: str, *, timeout: float, netns: str) -> dict[str, Any] | None:
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
            error = format_speed_test_error(
                (result.stderr or result.stdout or "curl speed test failed").strip()
            )
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
        if bytes_read <= 0:
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "mbps": 0.0,
                "error": "Speed test downloaded no data",
            }
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
            "error": format_speed_test_error(exc),
        }


def measure_upload_speed_curl(
    url: str,
    *,
    bytes_count: int,
    timeout: float,
    netns: str,
) -> dict[str, Any] | None:
    payload_size = max(0, int(bytes_count))
    started = time.perf_counter()
    # Pipe a fixed-size body into curl so measurement stays inside the VPN netns.
    cmd = (
        f"head -c {payload_size} /dev/zero | "
        f"ip netns exec {netns} curl -sS -L --max-time {max(1, int(timeout))} "
        f"-X POST --data-binary @- -o /dev/null -w '%{{size_upload}}' {subprocess.list2cmdline([url])}"
    )
    try:
        result = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
        duration_ms = max(int((time.perf_counter() - started) * 1000), 1)
        if result.returncode != 0:
            error = format_speed_test_error(
                (result.stderr or result.stdout or "curl upload speed test failed").strip()
            )
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "error": error,
            }
        bytes_sent = int(float(result.stdout.strip() or 0))
        # Prefer the requested payload size when curl reports 0 on some builds.
        transferred = bytes_sent if bytes_sent > 0 else payload_size
        megabits = (transferred * 8) / 1_000_000
        seconds = duration_ms / 1000
        mbps = round(megabits / seconds, 2) if seconds > 0 else None
        if transferred <= 0:
            return {
                "ok": False,
                "url": url,
                "bytes": 0,
                "duration_ms": duration_ms,
                "mbps": 0.0,
                "error": "Speed test uploaded no data",
            }
        return {
            "ok": True,
            "url": url,
            "bytes": transferred,
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
            "error": format_speed_test_error(exc),
        }




# Compatibility aliases for callers/tests that still use private names.
_format_speed_test_error = format_speed_test_error
_measure_download_speed = measure_download_speed
_measure_upload_speed = measure_upload_speed
_measure_download_speed_curl = measure_download_speed_curl
_measure_upload_speed_curl = measure_upload_speed_curl
