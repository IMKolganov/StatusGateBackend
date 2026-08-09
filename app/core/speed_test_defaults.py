"""Defaults for VPN throughput measurement.

Runtime code should call the getters so Settings/env changes apply without
re-importing modules. ``FALLBACK_*`` / legacy ``DEFAULT_*`` names remain for
SQLAlchemy server defaults and tests that only need a static string.
"""

from app.config import settings

FALLBACK_SPEED_TEST_URL_TEMPLATE = "https://speed.cloudflare.com/__down?bytes={bytes}"
FALLBACK_CLOUDFLARE_SPEED_TEST_ORIGIN = "https://speed.cloudflare.com"

# Static fallback aliases (DB server_default / older imports). Prefer getters at runtime.
DEFAULT_SPEED_TEST_URL_TEMPLATE = FALLBACK_SPEED_TEST_URL_TEMPLATE
CLOUDFLARE_SPEED_TEST_ORIGIN = FALLBACK_CLOUDFLARE_SPEED_TEST_ORIGIN

DEFAULT_SPEED_TEST_INTERVAL_SECONDS = 3600

SPEED_TEST_MIN_GAP_SECONDS = 60
CLOUDFLARE_SPEED_TEST_MIN_GAP_SECONDS = SPEED_TEST_MIN_GAP_SECONDS

SPEED_TEST_RATE_LIMIT_BACKOFF_SECONDS = 3600

CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE = 10


def default_speed_test_url_template() -> str:
    value = (settings.default_speed_test_url_template or "").strip()
    return value or FALLBACK_SPEED_TEST_URL_TEMPLATE


def cloudflare_speed_test_origin() -> str:
    value = (settings.cloudflare_speed_test_origin or "").strip()
    return (value or FALLBACK_CLOUDFLARE_SPEED_TEST_ORIGIN).rstrip("/")
