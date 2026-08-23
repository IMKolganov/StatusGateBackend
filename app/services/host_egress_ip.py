"""Cached public egress IP of the Status Gate checker host.

Used when HTTP checks fail with no status line so operators can update edge
allowlists without guessing the worker's outbound address.
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.probe_defaults import default_probe_url
from app.models.enums import IpFamily
from app.services.http_client import normalize_ip_family
from app.services.http_probe import _probe_endpoint

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300.0
_PROBE_TIMEOUT_SECONDS = 5.0

_lock = threading.Lock()
# Per ip_family so ipv4/ipv6/auto probes do not overwrite each other.
_cache: dict[str, tuple[str, float]] = {}


def get_checker_egress_ip(
    *,
    ip_family: str = IpFamily.AUTO.value,
    force_refresh: bool = False,
) -> str | None:
    """Return the host's public IP for the given family, or None if the probe fails."""
    family = normalize_ip_family(ip_family)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(family)
        if not force_refresh and cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
            return cached[0]

    probe = _probe_endpoint(
        default_probe_url(),
        _PROBE_TIMEOUT_SECONDS,
        ip_family=family,
    )
    exit_ip = probe.get("exit_ip")
    if not probe.get("ok") or not isinstance(exit_ip, str) or not exit_ip.strip():
        logger.debug(
            "Checker egress IP probe failed (family=%s): %s",
            family,
            probe.get("error") or probe,
        )
        return None

    value = exit_ip.strip()
    with _lock:
        _cache[family] = (value, time.monotonic())
    return value


def clear_egress_ip_cache() -> None:
    """Test helper: drop the in-process cache."""
    with _lock:
        _cache.clear()
