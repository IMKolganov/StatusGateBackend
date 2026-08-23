"""Validate DataGate Monitor base URLs (HTTPS + host allowlist)."""

from __future__ import annotations

from urllib.parse import urlparse

from app.config import settings


def allowed_datagate_hosts() -> set[str]:
    return {part.strip().lower() for part in settings.datagate_allowed_hosts.split(",") if part.strip()}


def validate_datagate_base_url(base_url: str) -> str:
    """Normalize and validate base_url. Raises ValueError on reject."""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("base_url is required")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("base_url must include a hostname")
    scheme = (parsed.scheme or "").lower()
    local = host in {"localhost", "127.0.0.1", "::1"}
    if scheme not in {"https", "http"}:
        raise ValueError("base_url must use https (or http for localhost)")
    if scheme == "http" and not local:
        raise ValueError("base_url must use https except for localhost")
    if host not in allowed_datagate_hosts():
        allowed = ", ".join(sorted(allowed_datagate_hosts())) or "(none)"
        raise ValueError(f"base_url host is not allowed; permitted: {allowed}")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not include credentials")
    # Rebuild without path noise beyond optional trailing path — keep path if present.
    netloc = parsed.netloc
    path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else ""
    return f"{scheme}://{netloc}{path}"
