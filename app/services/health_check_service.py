import json
import re
import time
from datetime import UTC, datetime

import httpx

from app.models.check_result import CheckResult
from app.models.enums import VPN_CHECK_TYPES, CheckOutcome, CheckType
from app.models.monitored_component import MonitoredComponent
from app.services.vpn_check_service import run_vpn_health_check

_XML_PREFIX_RE = re.compile(r"^\s*(<\?xml|<[!?])", re.IGNORECASE)
_XML_TAG_RE = re.compile(r"<\s*\w+[\s>]", re.IGNORECASE)


def _is_xml_body(body: str, content_type: str | None) -> bool:
    if content_type and ("xml" in content_type.lower() or "application/soap" in content_type.lower()):
        return True
    if not body.strip():
        return False
    return bool(_XML_PREFIX_RE.match(body) or _XML_TAG_RE.search(body[:500]))


def _evaluate_body(component: MonitoredComponent, response: httpx.Response) -> tuple[str, str | None, dict | None]:
    status_ok = response.status_code == component.expected_status_code
    content_type = response.headers.get("content-type", "")
    body_text = response.text
    details: dict = {
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


def run_health_check(component: MonitoredComponent) -> CheckResult:
    if component.check_type in VPN_CHECK_TYPES:
        return run_vpn_health_check(component)
    return _run_http_health_check(component)


def _run_http_health_check(component: MonitoredComponent) -> CheckResult:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)

    try:
        with httpx.Client(timeout=component.timeout_seconds, follow_redirects=True) as client:
            response = client.request(component.check_method.upper(), component.check_url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        outcome, error_message, details = _evaluate_body(component, response)
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
            details={"check_type": component.check_type},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=CheckOutcome.ERROR.value,
            latency_ms=latency_ms,
            http_status_code=None,
            error_message=str(exc),
            details={"check_type": component.check_type},
        )
