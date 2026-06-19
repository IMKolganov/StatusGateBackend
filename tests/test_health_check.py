from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.models.enums import CheckOutcome, CheckType
from app.models.monitored_component import MonitoredComponent
from app.services.health_check_service import run_health_check


def _component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "API",
        "slug": "api",
        "check_url": "https://example.com/health",
        "check_method": "GET",
        "check_type": CheckType.HTTP_STATUS.value,
        "expected_status_code": 200,
        "timeout_seconds": 5,
        "is_active": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


def _mock_response(*, status_code: int = 200, text: str = "{}", headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/health")
    return httpx.Response(status_code, text=text, headers=headers or {}, request=request)


class TestHealthCheckService:
    def test_http_status_up(self) -> None:
        component = _component(check_type=CheckType.HTTP_STATUS.value)
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(status_code=200)
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value
        assert result.http_status_code == 200

    def test_http_status_down(self) -> None:
        component = _component(check_type=CheckType.HTTP_STATUS.value, expected_status_code=200)
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(status_code=503)
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.DOWN.value

    def test_json_valid(self) -> None:
        component = _component(check_type=CheckType.JSON.value)
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='{"status":"ok"}',
                headers={"content-type": "application/json"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value

    def test_json_rejects_xml_body(self) -> None:
        component = _component(check_type=CheckType.JSON.value)
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='<?xml version="1.0"?><status>ok</status>',
                headers={"content-type": "application/xml"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.DOWN.value
        assert result.error_message is not None

    def test_xml_valid(self) -> None:
        component = _component(check_type=CheckType.XML.value)
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='<?xml version="1.0"?><health ok="true"/>',
                headers={"content-type": "application/xml"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value

    def test_timeout(self) -> None:
        component = _component()
        with patch("app.services.health_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.side_effect = httpx.TimeoutException("timeout")
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.TIMEOUT.value


class TestMonitoringApi:
    def test_manual_check_and_settings(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "Monitor me", "slug": "monitor-me", "description": None, "is_active": True},
        ).json()["data"]
        kind = client.post(
            "/api/admin/component-kinds",
            json={"name": "API", "slug": "api", "description": None},
        ).json()["data"]
        component = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Health",
                "slug": "health",
                "check_url": "https://httpbin.org/status/200",
                "check_type": "http_status",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "is_active": True,
            },
        ).json()["data"]

        settings = client.get("/api/admin/monitoring/settings")
        assert settings.status_code == 200
        assert settings.json()["data"]["default_poll_interval_seconds"] == 60

        check = client.post(f"/api/admin/monitoring/monitored-components/{component['id']}/check")
        assert check.status_code == 200, check.text
        body = check.json()["data"]
        assert body["outcome"] in {"up", "down", "error", "timeout"}
        assert body["latency_ms"] is not None

        patch = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_poll_interval_seconds": 120, "scheduler_interval_seconds": 15},
        )
        assert patch.status_code == 200
        assert patch.json()["data"]["default_poll_interval_seconds"] == 120
