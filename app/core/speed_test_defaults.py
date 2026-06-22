"""Defaults for VPN throughput measurement."""

# Official @cloudflare/speedtest downloadApiUrl with bytes query param:
# https://github.com/cloudflare/speedtest — default is https://speed.cloudflare.com/__down
DEFAULT_SPEED_TEST_URL_TEMPLATE = "https://speed.cloudflare.com/__down?bytes={bytes}"
DEFAULT_SPEED_TEST_INTERVAL_SECONDS = 3600

# Conservative UI hint when many VPN services poll speed.cloudflare.com from one server IP.
# Cloudflare does not document a fixed limit for this public endpoint; HTTP 429 is returned
# when request volume triggers WAF/rate limiting (see developers.cloudflare.com Error 429).
# A full browser speed test issues dozens of ramp-up requests; we issue one GET per check.
CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE = 10
