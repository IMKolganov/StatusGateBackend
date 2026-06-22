from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.check_result import CheckResult
from app.models.enums import CheckType
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings
from app.services.speed_test_config import (
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SpeedTestRunContext,
    build_speed_test_url,
    should_run_speed_test,
    speed_test_rate_warning,
    validate_speed_test_url_template,
)


def _settings(**overrides) -> MonitoringSettings:
    base = {
        "id": uuid4(),
        "default_poll_interval_seconds": 60,
        "scheduler_interval_seconds": 30,
        "default_speed_test_url_template": DEFAULT_SPEED_TEST_URL_TEMPLATE,
        "default_speed_test_interval_seconds": 3600,
    }
    base.update(overrides)
    return MonitoringSettings(**base)


def _vpn_component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "VPN",
        "slug": "vpn",
        "check_url": "https://ifconfig.me/ip",
        "check_method": "GET",
        "check_type": CheckType.OPENVPN.value,
        "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
        "expected_status_code": 200,
        "timeout_seconds": 30,
        "speed_test_enabled": True,
        "is_active": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


class TestSpeedTestConfig:
    def test_validate_speed_test_url_template_requires_bytes_placeholder(self) -> None:
        with pytest.raises(ValueError):
            validate_speed_test_url_template("https://example.com/download")

    def test_build_speed_test_url(self) -> None:
        url = build_speed_test_url("https://example.com/file?size={bytes}", 1024)
        assert url == "https://example.com/file?size=1024"

    def test_should_run_speed_test_respects_interval(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        settings = _settings(default_speed_test_interval_seconds=3600)
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(minutes=10),
            outcome="up",
            details={"network": {"speed_test": {"ok": True, "bytes": 1024}}},
        )
        assert should_run_speed_test(component, settings, latest) is False

    def test_speed_test_rate_warning_for_many_services(self) -> None:
        settings = _settings(default_poll_interval_seconds=60, default_speed_test_interval_seconds=0)
        components = [_vpn_component(slug=f"vpn-{index}") for index in range(12)]
        warning = speed_test_rate_warning(components, settings)
        assert warning is not None
        assert "speed.cloudflare.com" in warning
        assert str(len(components)) in warning

    def test_speed_test_context_default(self) -> None:
        context = SpeedTestRunContext.default()
        assert context.run_speed_test is True
        assert "{bytes}" in context.url_template
