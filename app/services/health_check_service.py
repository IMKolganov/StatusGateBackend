import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.models.check_result import CheckResult
from app.models.enums import VPN_CHECK_TYPES, CheckOutcome, CheckType, IpFamily
from app.models.monitored_component import MonitoredComponent
from app.services.host_egress_ip import get_checker_egress_ip
from app.services.http_client import httpx_client, normalize_ip_family
from app.services.speed_test_config import SpeedTestRunContext
from app.services.vpn_check_service import run_vpn_health_check

_XML_PREFIX_RE = re.compile(r"^\s*(<\?xml|<[!?])", re.IGNORECASE)
_XML_TAG_RE = re.compile(r"<\s*\w+[\s>]", re.IGNORECASE)

# Peer closed/reset with no HTTP status — often nginx `deny all` / edge allowlist,
# not an application outage (403 never arrives).
_NO_HTTP_RESPONSE_HINTS = (
    "disconnected without sending a response",
    "server disconnected",
    "connection reset",
    "connection closed",
    "remote end closed connection",
    "incomplete message",
    "peer closed connection",
)

FAILURE_MODE_NO_HTTP_RESPONSE = "no_http_response"


def _is_no_http_response(exc: BaseException) -> bool:
    """True when the peer closed/reset without producing an HTTP status line."""
    if isinstance(exc, httpx.RemoteProtocolError):
        return True
    message = str(exc).lower()
    return any(hint in message for hint in _NO_HTTP_RESPONSE_HINTS)


def _no_http_response_details(
    check_type: str,
    exc: BaseException,
    *,
    ip_family: str,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "check_type": check_type,
        "failure_mode": FAILURE_MODE_NO_HTTP_RESPONSE,
        "transport_error": str(exc),
        "ip_family": ip_family,
    }
    egress_ip = get_checker_egress_ip(ip_family=ip_family)
    if egress_ip:
        details["egress_ip"] = egress_ip
        details["network"] = {"probe": {"exit_ip": egress_ip}}
    return details


def _no_http_response_message(exc: BaseException, egress_ip: str | None, *, ip_family: str) -> str:
    base = (
        "No HTTP response — peer closed the connection without a status code "
        "(often an edge IP allowlist / nginx deny, not the origin app being down). "
        f"IP family: {ip_family}. Transport: {exc}"
    )
    if egress_ip:
        return f"{base} Checker egress IP: {egress_ip}."
    return base


def _is_xml_body(body: str, content_type: str | None) -> bool:
    if content_type and ("xml" in content_type.lower() or "application/soap" in content_type.lower()):
        return True
    if not body.strip():
        return False
    return bool(_XML_PREFIX_RE.match(body) or _XML_TAG_RE.search(body[:500]))


def _evaluate_body(component: MonitoredComponent, response: httpx.Response) -> tuple[str, str | None, dict[str, Any] | None]:
    status_ok = response.status_code == component.expected_status_code
    content_type = response.headers.get("content-type", "")
    body_text = response.text
    details: dict[str, Any] = {
        "check_type": component.check_type,
        "content_type": content_type or None,
        "body_preview": body_text[:500] if body_text else None,
    }

    if component.check_type == CheckType.HTTP_STATUS.value:
        if status_ok:
            return CheckOutcome.UP.value, None, details
        return (
            CheckOutcome.DOWN.value,
            f"Expected HTTP {component.expected_status_code}, got {response.status_code}",
            details,
        )

    if not status_ok:
        return (
            CheckOutcome.DOWN.value,
            f"Expected HTTP {component.expected_status_code}, got {response.status_code}",
            details,
        )

    if component.check_type == CheckType.JSON.value:
        try:
            parsed = json.loads(body_text) if body_text.strip() else None
        except json.JSONDecodeError as exc:
            return CheckOutcome.DOWN.value, f"Response is not valid JSON: {exc.msg}", details
        details["json_keys"] = list(parsed.keys())[:20] if isinstance(parsed, dict) else None
        if _is_xml_body(body_text, content_type):
            return CheckOutcome.DOWN.value, "Expected JSON body but response looks like XML", details
        return CheckOutcome.UP.value, None, details

    if component.check_type == CheckType.XML.value:
        if _is_xml_body(body_text, content_type):
            details["xml_detected"] = True
            return CheckOutcome.UP.value, None, details
        return CheckOutcome.DOWN.value, "Response is not valid XML", details

    return CheckOutcome.ERROR.value, f"Unknown check type: {component.check_type}", details


def run_health_check(
    component: MonitoredComponent,
    *,
    speed_test_context: SpeedTestRunContext | None = None,
) -> CheckResult:
    if component.check_type in VPN_CHECK_TYPES:
        return run_vpn_health_check(component, speed_test_context=speed_test_context)
    return _run_http_health_check(component)


def _run_http_health_check(component: MonitoredComponent) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    ip_family = normalize_ip_family(getattr(component, "ip_family", None) or IpFamily.AUTO.value)

    try:
        with httpx_client(timeout=component.timeout_seconds, ip_family=ip_family) as client:
            response = client.request(component.check_method.upper(), component.check_url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        outcome, error_message, details = _evaluate_body(component, response)
        if details is not None:
            details["ip_family"] = ip_family
        return CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=outcome,
            latency_ms=latency_ms,
            http_status_code=response.status_code,
            error_message=error_message,
            details=details,
        )
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=CheckOutcome.TIMEOUT.value,
            latency_ms=latency_ms,
            http_status_code=None,
            error_message=f"Request timed out after {component.timeout_seconds}s",
            details={"check_type": component.check_type, "ip_family": ip_family},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        if _is_no_http_response(exc):
            details = _no_http_response_details(component.check_type, exc, ip_family=ip_family)
            egress_ip = details.get("egress_ip")
            return CheckResult(
                monitored_component_id=component.id,
                checked_at=checked_at,
                # Keep outcome=error (not down): no status code arrived to compare.
                outcome=CheckOutcome.ERROR.value,
                latency_ms=latency_ms,
                http_status_code=None,
                error_message=_no_http_response_message(
                    exc,
                    egress_ip if isinstance(egress_ip, str) else None,
                    ip_family=ip_family,
                ),
                details=details,
            )
        return CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=CheckOutcome.ERROR.value,
            latency_ms=latency_ms,
            http_status_code=None,
            error_message=str(exc),
            details={"check_type": component.check_type, "ip_family": ip_family},
        )
