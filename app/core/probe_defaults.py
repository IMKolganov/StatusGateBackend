"""HTTP / DNS probe defaults from Settings (env with hardcoded fallbacks).

Prefer the getters at runtime so monkeypatched/reloaded Settings are visible
without restarting the process.
"""

from app.config import settings

FALLBACK_PROBE_URL = "https://ifconfig.me/ip"
FALLBACK_GOOGLE_PROBE_URL = "https://www.gstatic.com/generate_204"
FALLBACK_INTERNET_PING_HOST = "8.8.8.8"
FALLBACK_VPN_NETNS_DNS_SERVERS = ("1.1.1.1", "8.8.8.8")

# Static fallbacks for older imports; prefer getters below.
DEFAULT_PROBE_URL = FALLBACK_PROBE_URL
GOOGLE_PROBE_URL = FALLBACK_GOOGLE_PROBE_URL
INTERNET_PING_HOST = FALLBACK_INTERNET_PING_HOST


def default_probe_url() -> str:
    value = (settings.default_probe_url or "").strip()
    return value or FALLBACK_PROBE_URL


def google_probe_url() -> str:
    value = (settings.google_probe_url or "").strip()
    return value or FALLBACK_GOOGLE_PROBE_URL


def internet_ping_host() -> str:
    value = (settings.internet_ping_host or "").strip()
    return value or FALLBACK_INTERNET_PING_HOST


def vpn_netns_nameserver_lines() -> str:
    servers = [part.strip() for part in settings.vpn_netns_dns_servers.split(",") if part.strip()]
    if not servers:
        servers = list(FALLBACK_VPN_NETNS_DNS_SERVERS)
    return "".join(f"nameserver {server}\n" for server in servers)
