from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
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
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(status_code=200)
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value
        assert result.http_status_code == 200

    def test_http_status_down(self) -> None:
        component = _component(check_type=CheckType.HTTP_STATUS.value, expected_status_code=200)
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(status_code=503)
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.DOWN.value

    def test_json_valid(self) -> None:
        component = _component(check_type=CheckType.JSON.value)
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='{"status":"ok"}',
                headers={"content-type": "application/json"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value

    def test_json_rejects_xml_body(self) -> None:
        component = _component(check_type=CheckType.JSON.value)
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='<?xml version="1.0"?><status>ok</status>',
                headers={"content-type": "application/xml"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.DOWN.value
        assert result.error_message is not None

    def test_xml_valid(self) -> None:
        component = _component(check_type=CheckType.XML.value)
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(
                text='<?xml version="1.0"?><health ok="true"/>',
                headers={"content-type": "application/xml"},
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value

    def test_timeout(self) -> None:
        component = _component()
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.side_effect = httpx.TimeoutException("timeout")
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.TIMEOUT.value

    def test_no_http_response_classified_as_edge_denied_error(self) -> None:
        from app.services.health_check_service import FAILURE_MODE_NO_HTTP_RESPONSE
        from app.services.host_egress_ip import clear_egress_ip_cache

        clear_egress_ip_cache()
        component = _component(ip_family="ipv4")
        disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        with (
            patch("app.services.health_check_service.httpx_client") as client_cls,
            patch(
                "app.services.health_check_service.get_checker_egress_ip",
                return_value="203.0.113.10",
            ) as egress,
        ):
            client_cls.return_value.__enter__.return_value.request.side_effect = disconnect
            result = run_health_check(component)

        egress.assert_called_once_with(ip_family="ipv4")
        client_cls.assert_called_once()
        assert client_cls.call_args.kwargs.get("ip_family") == "ipv4"
        assert result.outcome == CheckOutcome.ERROR.value
        assert result.http_status_code is None
        assert result.details is not None
        assert result.details["failure_mode"] == FAILURE_MODE_NO_HTTP_RESPONSE
        assert result.details["egress_ip"] == "203.0.113.10"
        assert result.details["ip_family"] == "ipv4"
        assert result.details["network"]["probe"]["exit_ip"] == "203.0.113.10"
        assert result.error_message is not None
        assert "No HTTP response" in result.error_message
        assert "edge IP allowlist" in result.error_message
        assert "203.0.113.10" in result.error_message

    def test_generic_http_error_unchanged(self) -> None:
        component = _component()
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.side_effect = httpx.ConnectError(
                "All connection attempts failed"
            )
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.ERROR.value
        assert result.error_message == "All connection attempts failed"
        assert result.details == {"check_type": component.check_type, "ip_family": "auto"}

    def test_connection_reset_message_is_no_http_response(self) -> None:
        from app.services.health_check_service import FAILURE_MODE_NO_HTTP_RESPONSE

        component = _component(ip_family="ipv6")
        with (
            patch("app.services.health_check_service.httpx_client") as client_cls,
            patch("app.services.health_check_service.get_checker_egress_ip", return_value=None),
        ):
            client_cls.return_value.__enter__.return_value.request.side_effect = httpx.ConnectError(
                "Connection reset by peer"
            )
            result = run_health_check(component)

        assert result.outcome == CheckOutcome.ERROR.value
        assert result.details is not None
        assert result.details["failure_mode"] == FAILURE_MODE_NO_HTTP_RESPONSE
        assert result.details["ip_family"] == "ipv6"
        assert client_cls.call_args.kwargs.get("ip_family") == "ipv6"
        assert result.error_message is not None
        assert "IP family: ipv6" in result.error_message

    def test_successful_check_records_ip_family(self) -> None:
        component = _component(ip_family="ipv4")
        with patch("app.services.health_check_service.httpx_client") as client_cls:
            client_cls.return_value.__enter__.return_value.request.return_value = _mock_response(status_code=200)
            result = run_health_check(component)
        assert result.outcome == CheckOutcome.UP.value
        assert result.details is not None
        assert result.details["ip_family"] == "ipv4"
        assert client_cls.call_args.kwargs.get("ip_family") == "ipv4"


class TestHttpClientIpFamily:
    def test_transport_local_address(self) -> None:
        from app.services.http_client import http_transport, normalize_ip_family

        assert normalize_ip_family(None) == "auto"
        assert normalize_ip_family("bogus") == "auto"
        assert normalize_ip_family("ipv4") == "ipv4"

        with patch("app.services.http_client.httpx.HTTPTransport") as transport_cls:
            http_transport(ip_family="ipv4")
            assert transport_cls.call_args.kwargs["local_address"] == "0.0.0.0"
            http_transport(ip_family="ipv6")
            assert transport_cls.call_args.kwargs["local_address"] == "::"
            http_transport(ip_family="auto")
            assert "local_address" not in transport_cls.call_args.kwargs
            http_transport(ip_family="ipv4", proxy="socks5://127.0.0.1:1080")
            assert transport_cls.call_args.kwargs["proxy"] == "socks5://127.0.0.1:1080"
            assert transport_cls.call_args.kwargs["local_address"] == "0.0.0.0"


class TestHostEgressIp:
    def test_caches_per_ip_family(self) -> None:
        from app.services import host_egress_ip as egress

        egress.clear_egress_ip_cache()
        with patch(
            "app.services.host_egress_ip._probe_endpoint",
            side_effect=[
                {"ok": True, "exit_ip": "203.0.113.10"},
                {"ok": True, "exit_ip": "2001:db8::1"},
            ],
        ) as probe:
            assert egress.get_checker_egress_ip(ip_family="ipv4") == "203.0.113.10"
            assert egress.get_checker_egress_ip(ip_family="ipv4") == "203.0.113.10"
            assert egress.get_checker_egress_ip(ip_family="ipv6") == "2001:db8::1"
            assert probe.call_count == 2
            assert probe.call_args_list[0].kwargs["ip_family"] == "ipv4"
            assert probe.call_args_list[1].kwargs["ip_family"] == "ipv6"

    def test_probe_failure_returns_none(self) -> None:
        from app.services import host_egress_ip as egress

        egress.clear_egress_ip_cache()
        with patch(
            "app.services.host_egress_ip._probe_endpoint",
            return_value={"ok": False, "error": "boom"},
        ):
            assert egress.get_checker_egress_ip(ip_family="auto") is None


class TestHttpProbeIpFamily:
    def test_curl_adds_family_flags(self) -> None:
        from app.services.http_probe import _probe_endpoint_via_curl

        with patch("app.services.http_probe.subprocess.run") as run:
            run.return_value = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "203.0.113.10\n__HTTP_CODE__:200", "stderr": ""},
            )()
            result = _probe_endpoint_via_curl(
                "https://ifconfig.me/ip",
                5,
                netns="sg-test",
                ip_family="ipv4",
            )
        assert result["ok"] is True
        assert result["exit_ip"] == "203.0.113.10"
        cmd = run.call_args.args[0]
        assert "-4" in cmd
        assert "-6" not in cmd

        with patch("app.services.http_probe.subprocess.run") as run:
            run.return_value = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "2001:db8::1\n__HTTP_CODE__:200", "stderr": ""},
            )()
            _probe_endpoint_via_curl(
                "https://ifconfig.me/ip",
                5,
                netns="sg-test",
                ip_family="ipv6",
            )
        assert "-6" in run.call_args.args[0]


class TestIpFamilySchemasAndApi:
    def test_create_schema_defaults_and_rejects_invalid(self) -> None:
        from pydantic import ValidationError

        from app.schemas.monitored_component import MonitoredComponentCreate

        payload = MonitoredComponentCreate.model_validate(
            {
                "project_id": uuid4(),
                "component_kind_id": uuid4(),
                "name": "API",
                "slug": "api",
                "check_url": "https://example.com/health",
            }
        )
        assert payload.ip_family == "auto"

        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "API",
                    "slug": "api-bad",
                    "check_url": "https://example.com/health",
                    "ip_family": "dual",
                }
            )

    def test_create_and_update_ip_family_via_api(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "IP family", "slug": "ip-family", "description": None, "is_active": True},
            headers=admin_headers,
        ).json()["data"]
        kind = client.post(
            "/api/admin/component-kinds",
            json={"name": "API", "slug": "api-ip-family", "description": None},
            headers=admin_headers,
        ).json()["data"]
        created = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Allowlisted",
                "slug": "allowlisted",
                "check_url": "https://example.com/health",
                "check_type": "http_status",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "ip_family": "ipv4",
                "is_active": True,
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert body["ip_family"] == "ipv4"

        updated = client.patch(
            f"/api/admin/monitored-components/{body['id']}",
            json={"ip_family": "ipv6"},
            headers=admin_headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["ip_family"] == "ipv6"

        listed = client.get(
            "/api/admin/monitored-components",
            params={"project_id": project["id"]},
            headers=admin_headers,
        )
        item = next(row for row in listed.json()["data"]["items"] if row["id"] == body["id"])
        assert item["ip_family"] == "ipv6"


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

    def test_purge_check_history(self, client: TestClient, admin_headers: dict, db_session) -> None:
        from app.models.check_result import CheckResult
        from app.models.enums import CheckOutcome

        project = client.post(
            "/api/admin/projects",
            json={"name": "Purge me", "slug": "purge-me", "description": None, "is_active": True},
        ).json()["data"]
        kind = client.post(
            "/api/admin/component-kinds",
            json={"name": "API", "slug": "api-purge", "description": None},
        ).json()["data"]
        component = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Health",
                "slug": "health-purge",
                "check_url": "https://httpbin.org/status/200",
                "check_type": "http_status",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "is_active": True,
            },
        ).json()["data"]

        for idx in range(5):
            db_session.add(
                CheckResult(
                    monitored_component_id=component["id"],
                    checked_at=datetime.now(UTC),
                    outcome=CheckOutcome.UP.value,
                    latency_ms=100 + idx,
                )
            )
        db_session.commit()

        response = client.delete(
            f"/api/admin/monitoring/monitored-components/{component['id']}/check-results",
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["deleted_count"] == 5
        assert body["remaining_count"] == 0

        listed = client.get(
            "/api/admin/monitored-components",
            params={"project_id": project["id"]},
            headers=admin_headers,
        )
        item = next(row for row in listed.json()["data"]["items"] if row["id"] == component["id"])
        assert item["latest_outcome"] is None
