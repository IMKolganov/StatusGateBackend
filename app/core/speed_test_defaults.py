"""Defaults for VPN throughput measurement."""

# Official @cloudflare/speedtest downloadApiUrl with bytes query param:
# https://github.com/cloudflare/speedtest — default is https://speed.cloudflare.com/__down
DEFAULT_SPEED_TEST_URL_TEMPLATE = "https://speed.cloudflare.com/__down?bytes={bytes}"
DEFAULT_SPEED_TEST_INTERVAL_SECONDS = 3600

# Minimum gap between Cloudflare speed test GETs from this worker (all VPN services share one egress IP).
CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS = 60

# After HTTP 429, wait at least this long before retrying (per service).
SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS = 3600

# Conservative UI hint when many VPN services poll speed.cloudflare.com from one server IP.
CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE = 10
