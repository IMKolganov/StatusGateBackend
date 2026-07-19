"""Speed test settings: config helpers, schemas, VPN cache path, monitoring API."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID, WEB_COMPONENT_KIND_ID
from app.models.enums import CheckType
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings
from app.schemas.monitored_component import MonitoredComponentCreate
from app.schemas.monitoring import MonitoringSettingsUpdate
from app.services import vpn_check_service as vpn
from app.services.monitoring_admin_service import MonitoringAdminService
from app.services.monitoring_service import HealthCheckRunner
from app.services.speed_test_config import (
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SpeedTestRunContext,
    effective_speed_test_interval_seconds,
    effective_speed_test_url_template,
    estimate_speed_tests_per_minute,
    extract_speed_test_from_details,
    should_run_speed_test,
    speed_test_rate_warning,
    uses_default_cloudflare_template,
    validate_speed_test_url_template,
)


def _settings(**overrides) -> MonitoringSettings:
    base = {
        "id": MONITORING_SETTINGS_ID,
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


def _api_data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Speed test project", "slug": "speed-test-proj", "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _api_data(response)


def _create_vpn_component(
    client: TestClient,
    *,
    project_id: str,
    slug: str = "vpn-node",
    **extra_fields,
) -> dict:
    payload = {
        "project_id": project_id,
        "component_kind_id": str(OPENVPN_COMPONENT_KIND_ID),
        "name": "VPN node",
        "slug": slug,
        "check_type": "openvpn",
        "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
        "timeout_seconds": 60,
        "is_active": True,
        **extra_fields,
    }
    response = client.post("/api/admin/monitored-components", json=payload)
    assert response.status_code == 201, response.text
    return _api_data(response)


class TestSpeedTestConfigExtended:
    def test_effective_speed_test_url_template_prefers_component_override(self) -> None:
        component = _vpn_component(speed_test_url_template="https://cdn.example.com/dl?n={bytes}")
        settings = _settings()
        assert effective_speed_test_url_template(component, settings) == "https://cdn.example.com/dl?n={bytes}"

    def test_effective_speed_test_url_template_falls_back_to_settings(self) -> None:
        component = _vpn_component(speed_test_url_template=None)
        settings = _settings(default_speed_test_url_template="https://cdn.example.com/default?b={bytes}")
        assert effective_speed_test_url_template(component, settings) == "https://cdn.example.com/default?b={bytes}"

    def test_effective_speed_test_interval_seconds(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=900)
        settings = _settings(default_speed_test_interval_seconds=3600)
        assert effective_speed_test_interval_seconds(component, settings) == 900
        assert effective_speed_test_interval_seconds(_vpn_component(speed_test_interval_seconds=None), settings) == 3600

    def test_uses_default_cloudflare_template(self) -> None:
        settings = _settings()
        assert uses_default_cloudflare_template(_vpn_component(), settings) is True
        assert (
            uses_default_cloudflare_template(
                _vpn_component(speed_test_url_template="https://cdn.example.com/x?b={bytes}"),
                settings,
            )
            is False
        )

    def test_extract_speed_test_from_details(self) -> None:
        details = {"network": {"speed_test": {"ok": True, "mbps": 12.5}}}
        assert extract_speed_test_from_details(details) == {"ok": True, "mbps": 12.5}
        assert extract_speed_test_from_details({"network": {}}) is None
        assert extract_speed_test_from_details(None) is None

    def test_should_run_speed_test_when_disabled(self) -> None:
        component = _vpn_component(speed_test_enabled=False)
        assert should_run_speed_test(component, _settings(), None) is False

    def test_should_run_speed_test_when_interval_zero(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=0)
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(minutes=1),
            outcome="up",
            details={"network": {"speed_test": {"ok": True}}},
        )
        assert should_run_speed_test(component, _settings(), latest) is True

    def test_should_run_speed_test_when_no_prior_result(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        assert should_run_speed_test(component, _settings(), None) is True

    def test_should_run_speed_test_when_prior_lacks_speed_test(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(hours=2),
            outcome="up",
            details={"network": {"probe": {"ok": True}}},
        )
        assert should_run_speed_test(component, _settings(), latest) is True

    def test_should_run_speed_test_when_interval_elapsed(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        checked_at = datetime.now(UTC) - timedelta(hours=2)
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome="up",
            details={"network": {"speed_test": {"ok": True}}},
        )
        assert should_run_speed_test(component, _settings(), latest, now=datetime.now(UTC)) is True

    def test_estimate_speed_tests_per_minute_respects_speed_interval(self) -> None:
        settings = _settings(default_poll_interval_seconds=60, default_speed_test_interval_seconds=3600)
        components = [_vpn_component(slug="vpn-a", poll_interval_seconds=60, speed_test_interval_seconds=3600)]
        assert estimate_speed_tests_per_minute(components, settings) == pytest.approx(1 / 60, rel=1e-6)

    def test_speed_test_rate_warning_skips_custom_url(self) -> None:
        settings = _settings(default_speed_test_interval_seconds=0)
        components = [
            _vpn_component(
                slug=f"vpn-{index}",
                speed_test_url_template="https://cdn.example.com/file?size={bytes}",
            )
            for index in range(12)
        ]
        assert speed_test_rate_warning(components, settings) is None

    def test_validate_speed_test_url_template_rejects_http(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            validate_speed_test_url_template("http://example.com/{bytes}")


class TestSpeedTestSchemas:
    def test_vpn_create_accepts_speed_test_settings(self) -> None:
        payload = MonitoredComponentCreate.model_validate(
            {
                "project_id": uuid4(),
                "component_kind_id": uuid4(),
                "name": "VPN",
                "slug": "vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                "speed_test_url_template": "https://cdn.example.com/x?b={bytes}",
                "speed_test_interval_seconds": 1800,
                "speed_test_enabled": False,
            }
        )
        assert payload.speed_test_url_template == "https://cdn.example.com/x?b={bytes}"
        assert payload.speed_test_interval_seconds == 1800
        assert payload.speed_test_enabled is False

    def test_http_create_rejects_speed_test_url_template(self) -> None:
        with pytest.raises(ValidationError, match="speed_test_url_template"):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "API",
                    "slug": "api",
                    "check_url": "https://example.com",
                    "speed_test_url_template": "https://cdn.example.com/x?b={bytes}",
                }
            )

    def test_monitoring_settings_update_validates_url_template(self) -> None:
        with pytest.raises(ValidationError):
            MonitoringSettingsUpdate.model_validate({"default_speed_test_url_template": "https://example.com/no-bytes"})

        payload = MonitoringSettingsUpdate.model_validate(
            {"default_speed_test_url_template": "https://cdn.example.com/x?b={bytes}"}
        )
        assert payload.default_speed_test_url_template == "https://cdn.example.com/x?b={bytes}"


class TestEnrichNetworkMetricsSpeedTest:
    def test_reuses_cached_speed_test_when_skipped(self) -> None:
        network: dict = {}
        previous = {"ok": True, "mbps": 42.0, "bytes": 524288}
        context = SpeedTestRunContext(
            url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE,
            run_speed_test=False,
            previous_speed_test=previous,
        )
        with patch("app.services.vpn_check_service._measure_download_speed") as measure:
            vpn._enrich_network_metrics(
                network,
                gateway=None,
                proxy_url=None,
                iface=None,
                timeout=30,
                speed_test_context=context,
            )
            measure.assert_not_called()
        assert network["speed_test"]["mbps"] == 42.0
        assert network["speed_test"]["cached"] is True

    def test_cloudflare_throttle_reuses_last_success(self) -> None:
        from app.services.speed_test_config import reset_cloudflare_speed_test_slot_for_tests

        reset_cloudflare_speed_test_slot_for_tests()
        network: dict = {}
        last_success = {"ok": True, "mbps": 25.0, "bytes": 524288, "duration_ms": 900}
        context = SpeedTestRunContext(
            url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE,
            run_speed_test=True,
            previous_speed_test={"ok": False, "error": "Speed test rate limited (HTTP 429)"},
            last_successful_speed_test=last_success,
        )
        with (
            patch("app.services.vpn_check_service.try_acquire_speed_test_slot", return_value=False),
            patch("app.services.vpn_check_service._measure_download_speed") as measure,
        ):
            vpn._enrich_network_metrics(
                network,
                gateway=None,
                proxy_url=None,
                iface=None,
                timeout=30,
                speed_test_context=context,
            )
            measure.assert_not_called()
        assert network["speed_test"]["mbps"] == 25.0
        assert network["speed_test"]["stale"] is True
        assert network["speed_test"]["throttled"] is True

    def test_runs_download_when_enabled(self) -> None:
        from app.services.speed_test_config import reset_cloudflare_speed_test_slot_for_tests

        reset_cloudflare_speed_test_slot_for_tests()
        network: dict = {}
        context = SpeedTestRunContext(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)
        measured = {"ok": True, "mbps": 10.0, "bytes": 524288, "url": "https://speed.cloudflare.com/__down?bytes=524288"}
        with patch("app.services.vpn_check_service._measure_download_speed", return_value=measured) as measure:
            vpn._enrich_network_metrics(
                network,
                gateway=None,
                proxy_url="socks5://127.0.0.1:1080",
                iface=None,
                timeout=30,
                speed_test_context=context,
            )
            measure.assert_called_once()
        assert network["speed_test"]["ok"] is True
        assert network["speed_test"]["mbps"] == 10.0
        assert isinstance(network["speed_test"].get("measured_at"), str)
        assert network["speed_test_last_success"] == network["speed_test"]


class TestHealthCheckRunnerSpeedTestContext:
    def test_run_check_builds_speed_test_context(self, db_session: Session) -> None:
        from app.models.project import Project

        project = Project(name="Runner", slug="runner-proj", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()

        component = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="VPN runner",
            slug="vpn-runner",
            check_url="https://ifconfig.me/ip",
            check_method="GET",
            check_type=CheckType.OPENVPN.value,
            check_config={"config_text": "client\ndev tun\nremote x 1194\n"},
            expected_status_code=200,
            timeout_seconds=60,
            speed_test_url_template="https://cdn.example.com/x?b={bytes}",
            speed_test_interval_seconds=3600,
            speed_test_enabled=True,
            is_active=True,
        )
        db_session.add(component)
        db_session.commit()

        runner = HealthCheckRunner(db_session)
        captured: dict = {}

        def fake_run_health_check(comp, *, speed_test_context=None):
            captured["context"] = speed_test_context
            return CheckResult(
                monitored_component_id=comp.id,
                checked_at=datetime.now(UTC),
                outcome="up",
                latency_ms=100,
            )

        with patch("app.services.monitoring_service.run_health_check", side_effect=fake_run_health_check):
            runner.run_check(component)

        context = captured["context"]
        assert context is not None
        assert context.url_template == "https://cdn.example.com/x?b={bytes}"
        assert context.run_speed_test is True
        assert context.previous_speed_test is None

    def test_run_due_checks_staggers_speed_tests_to_one_vpn(self, db_session: Session) -> None:
        from app.models.project import Project

        project = Project(name="Stagger", slug="stagger-proj", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()

        components = []
        for index in range(3):
            component = MonitoredComponent(
                project_id=project.id,
                component_kind_id=OPENVPN_COMPONENT_KIND_ID,
                name=f"VPN {index}",
                slug=f"vpn-stagger-{index}",
                check_url="https://ifconfig.me/ip",
                check_method="GET",
                check_type=CheckType.OPENVPN.value,
                check_config={"config_text": "client\ndev tun\nremote x 1194\n"},
                expected_status_code=200,
                timeout_seconds=60,
                speed_test_interval_seconds=3600,
                speed_test_enabled=True,
                is_active=True,
                last_checked_at=datetime.now(UTC) - timedelta(hours=2),
            )
            db_session.add(component)
            components.append(component)
        db_session.flush()

        measured_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        for component in components:
            db_session.add(
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=datetime.now(UTC) - timedelta(hours=2),
                    outcome="up",
                    details={
                        "network": {
                            "speed_test": {
                                "ok": True,
                                "mbps": 10.0,
                                "measured_at": measured_at,
                            }
                        }
                    },
                )
            )
        db_session.commit()

        runner = HealthCheckRunner(db_session)
        contexts: list[SpeedTestRunContext] = []

        def fake_run_health_check(comp, *, speed_test_context=None):
            if speed_test_context is not None:
                contexts.append(speed_test_context)
            return CheckResult(
                monitored_component_id=comp.id,
                checked_at=datetime.now(UTC),
                outcome="up",
                latency_ms=100,
            )

        with patch("app.services.monitoring_service.run_health_check", side_effect=fake_run_health_check):
            runner.run_due_checks()

        assert len(contexts) == 3
        assert sum(1 for context in contexts if context.run_speed_test) == 1


class TestSpeedTestAdvisoryService:
    def test_get_speed_test_advisory_counts_project_vpn_services(self, db_session: Session) -> None:
        from app.models.project import Project

        project_a = Project(name="A", slug="proj-a", description=None, is_active=True)
        project_b = Project(name="B", slug="proj-b", description=None, is_active=True)
        db_session.add_all([project_a, project_b])
        db_session.flush()

        for index in range(3):
            db_session.add(
                MonitoredComponent(
                    project_id=project_a.id,
                    component_kind_id=OPENVPN_COMPONENT_KIND_ID,
                    name=f"VPN {index}",
                    slug=f"vpn-a-{index}",
                    check_url="https://ifconfig.me/ip",
                    check_method="GET",
                    check_type=CheckType.OPENVPN.value,
                    check_config={"config_text": "client\ndev tun\nremote x 1194\n"},
                    expected_status_code=200,
                    timeout_seconds=60,
                    speed_test_enabled=True,
                    is_active=True,
                )
            )
        db_session.add(
            MonitoredComponent(
                project_id=project_b.id,
                component_kind_id=OPENVPN_COMPONENT_KIND_ID,
                name="VPN B",
                slug="vpn-b-0",
                check_url="https://ifconfig.me/ip",
                check_method="GET",
                check_type=CheckType.OPENVPN.value,
                check_config={"config_text": "client\ndev tun\nremote x 1194\n"},
                expected_status_code=200,
                timeout_seconds=60,
                speed_test_enabled=True,
                is_active=True,
            )
        )
        db_session.add(
            MonitoredComponent(
                project_id=project_a.id,
                component_kind_id=WEB_COMPONENT_KIND_ID,
                name="HTTP",
                slug="http-a",
                check_url="https://example.com",
                check_method="GET",
                check_type=CheckType.HTTP_STATUS.value,
                expected_status_code=200,
                timeout_seconds=10,
                is_active=True,
            )
        )
        db_session.commit()

        service = MonitoringAdminService(db_session)
        all_advisory = service.get_speed_test_advisory()
        project_advisory = service.get_speed_test_advisory(project_id=project_a.id)

        assert all_advisory.active_vpn_service_count == 4
        assert project_advisory.active_vpn_service_count == 3
        assert project_advisory.uses_default_cloudflare_template is True
        assert project_advisory.guidance_requests_per_minute == 10


class TestSpeedTestMonitoringApi:
    def test_settings_include_speed_test_defaults(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get("/api/admin/monitoring/settings")
        assert response.status_code == 200
        data = _api_data(response)
        assert "default_speed_test_url_template" in data
        assert "{bytes}" in data["default_speed_test_url_template"]
        assert data["default_speed_test_interval_seconds"] == 3600

    def test_update_speed_test_settings(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={
                "default_speed_test_url_template": "https://cdn.example.com/file?size={bytes}",
                "default_speed_test_interval_seconds": 7200,
            },
        )
        assert response.status_code == 200, response.text
        data = _api_data(response)
        assert data["default_speed_test_url_template"] == "https://cdn.example.com/file?size={bytes}"
        assert data["default_speed_test_interval_seconds"] == 7200

    def test_update_speed_test_settings_rejects_invalid_template(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_speed_test_url_template": "https://example.com/no-placeholder"},
        )
        assert response.status_code == 422

    def test_speed_test_advisory_endpoint(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client)
        _create_vpn_component(client, project_id=project["id"], slug="vpn-one")
        _create_vpn_component(
            client,
            project_id=project["id"],
            slug="vpn-two",
            speed_test_url_template="https://cdn.example.com/x?b={bytes}",
        )

        global_response = client.get("/api/admin/monitoring/speed-test-advisory")
        assert global_response.status_code == 200
        global_data = _api_data(global_response)
        assert global_data["active_vpn_service_count"] >= 2
        assert "estimated_speed_tests_per_minute" in global_data

        scoped = client.get("/api/admin/monitoring/speed-test-advisory", params={"project_id": project["id"]})
        assert scoped.status_code == 200
        scoped_data = _api_data(scoped)
        assert scoped_data["active_vpn_service_count"] == 2

    def test_create_vpn_component_with_speed_test_fields(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client)
        created = _create_vpn_component(
            client,
            project_id=project["id"],
            slug="vpn-custom",
            speed_test_url_template="https://cdn.example.com/x?b={bytes}",
            speed_test_interval_seconds=1800,
            speed_test_enabled=False,
        )
        assert created["speed_test_url_template"] == "https://cdn.example.com/x?b={bytes}"
        assert created["speed_test_interval_seconds"] == 1800
        assert created["speed_test_enabled"] is False

    def test_http_component_rejects_speed_test_fields(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client)
        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": str(WEB_COMPONENT_KIND_ID),
                "name": "API",
                "slug": "api-speed",
                "check_url": "https://example.com/health",
                "check_type": "http_status",
                "speed_test_enabled": True,
            },
        )
        assert response.status_code == 422
