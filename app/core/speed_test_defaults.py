"""Defaults for VPN throughput measurement.

Values come from Settings / env when set; hardcoded strings below are fallbacks only.
"""

from app.config import settings

# Official @cloudflare/speedtest downloadApiUrl with bytes query param:
# https://github.com/cloudflare/speedtest — default is https://speed.cloudflare.com/__down
DEFAULT_SPEED_TEST_URL_TEMPLATE = settings.default_speed_test_url_template
CLOUDFLARE_SPEED_TEST_ORIGIN = settings.cloudflare_speed_test_origin.rstrip("/")

DEFAULT_SPEED_TEST_INTERVAL_SECONDS = 3600

# Minimum gap between live speed tests from this worker (all VPN services share one process).
# Cloudflare and custom URLs both use this so checks are staggered, not burst together.
SPEED_TEST_MIN_GAP_SECONDS = 60
# Back-compat alias used by guidance copy / older imports.
CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS = SPEED_TEST_MIN_GAP_SECONDS

# After HTTP 429, wait at least this long before retrying (per service).
SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS = 3600

# Conservative UI hint when many VPN services poll the Cloudflare speed endpoint from one server IP.
CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE = 10
