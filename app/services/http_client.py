"""Shared httpx client helpers for Status Gate checkers."""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import IpFamily

# Bind local_address to force A vs AAAA. Omit for IpFamily.AUTO (Happy Eyeballs).
_LOCAL_ADDRESS_BY_FAMILY = {
    IpFamily.IPV4.value: "0.0.0.0",
    IpFamily.IPV6.value: "::",
}


def normalize_ip_family(value: str | None) -> str:
    if value in {IpFamily.IPV4.value, IpFamily.IPV6.value, IpFamily.AUTO.value}:
        return value
    return IpFamily.AUTO.value


def http_transport(*, ip_family: str = IpFamily.AUTO.value, proxy: str | None = None) -> httpx.HTTPTransport:
    family = normalize_ip_family(ip_family)
    kwargs: dict[str, Any] = {}
    local_address = _LOCAL_ADDRESS_BY_FAMILY.get(family)
    if local_address is not None:
        kwargs["local_address"] = local_address
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.HTTPTransport(**kwargs)


def httpx_client(
    *,
    timeout: float | httpx.Timeout,
    follow_redirects: bool = True,
    proxy: str | None = None,
    ip_family: str = IpFamily.AUTO.value,
) -> httpx.Client:
    return httpx.Client(
        transport=http_transport(ip_family=ip_family, proxy=proxy),
        timeout=timeout,
        follow_redirects=follow_redirects,
    )


# Back-compat alias used briefly while forcing IPv4-only.
def ipv4_httpx_client(
    *,
    timeout: float | httpx.Timeout,
    follow_redirects: bool = True,
    proxy: str | None = None,
) -> httpx.Client:
    return httpx_client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        proxy=proxy,
        ip_family=IpFamily.IPV4.value,
    )
