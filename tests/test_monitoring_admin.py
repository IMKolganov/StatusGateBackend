from datetime import UTC, datetime
from uuid import uuid4

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome
from app.models.monitored_component import MonitoredComponent
from app.services.monitoring_admin_service import MonitoringAdminService


def _component() -> MonitoredComponent:
    now = datetime.now(UTC)
    return MonitoredComponent(
        id=uuid4(),
        project_id=uuid4(),
        component_kind_id=uuid4(),
        name="Norway VPN",
        slug="norway-vpn",
        check_url="https://ifconfig.me/ip",
        check_method="GET",
        check_type="openvpn",
        expected_status_code=200,
        timeout_seconds=60,
        is_active=True,
        speed_test_enabled=True,
        created_at=now,
        updated_at=now,
    )


class TestMonitoringAdminEnrich:
    def test_enrich_component_includes_error_and_logs(self) -> None:
        component = _component()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.TIMEOUT.value,
            latency_ms=60294,
            error_message="OpenVPN tunnel did not come up in time: Authentication failed (AUTH_FAILED)",
            details={
                "check_type": "openvpn",
                "log_tail": "AUTH: Received control message: AUTH_FAILED\n",
            },
        )
        response = MonitoringAdminService.enrich_component(component, latest)
        assert response.latest_outcome == "timeout"
        assert response.latest_error_message == latest.error_message
        assert response.latest_log_tail == "AUTH: Received control message: AUTH_FAILED\n"

    def test_enrich_component_without_latest(self) -> None:
        component = _component()
        response = MonitoringAdminService.enrich_component(component, None)
        assert response.latest_outcome is None
        assert response.latest_error_message is None
        assert response.latest_log_tail is None
