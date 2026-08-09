"""HTTP / DNS probe defaults from Settings (env with hardcoded fallbacks)."""

from app.config import settings

DEFAULT_PROBE_URL = settings.default_probe_url
GOOGLE_PROBE_URL = settings.google_probe_url
INTERNET_PING_HOST = settings.internet_ping_host


def vpn_netns_nameserver_lines() -> str:
    servers = [part.strip() for part in settings.vpn_netns_dns_servers.split(",") if part.strip()]
    if not servers:
        servers = ["1.1.1.1", "8.8.8.8"]
    return "".join(f"nameserver {server}\n" for server in servers)
