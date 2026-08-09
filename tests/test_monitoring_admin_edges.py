"""Monitoring admin API edges: purge, events, settings, advisory, manual check, due skips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID, WEB_COMPONENT_KIND_ID
from app.models.connection_event import ConnectionEvent
from app.models.enums import CheckOutcome, CheckType, ConnectionEventType, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.services.monitoring_service import HealthCheckRunner
from app.services.speed_test_config import DEFAULT_SPEED_TEST_URL_TEMPLATE


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_project(client: TestClient, slug: str = "mon-edges") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Monitoring Edges", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_http_component(client: TestClient, *, project_id: str, slug: str = "http-svc") -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": str(WEB_COMPONENT_KIND_ID),
            "name": "HTTP svc",
            "slug": slug,
            "check_url": "https://example.com/health",
            "check_type": "http_status",
            "check_method": "GET",
            "expected_status_code": 200,
            "timeout_seconds": 10,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_vpn_component(
    client: TestClient,
    *,
    project_id: str,
    slug: str = "vpn-svc",
    **extra,
) -> dict:
    payload = {
        "project_id": project_id,
        "component_kind_id": str(OPENVPN_COMPONENT_KIND_ID),
        "name": "VPN svc",
        "slug": slug,
        "check_type": "openvpn",
        "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
        "timeout_seconds": 30,
        "is_active": True,
        **extra,
    }
    response = client.post("/api/admin/monitored-components", json=payload)
    assert response.status_code == 201, response.text
    return _data(response)


class TestPurgeCheckHistoryEdges:
    def test_purge_ok_with_keep(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        project = _create_project(client, slug="purge-keep")
        component = _create_http_component(client, project_id=project["id"], slug="purge-keep-svc")
        base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        for idx in range(5):
            db_session.add(
                CheckResult(
                    monitored_component_id=component["id"],
                    checked_at=base + timedelta(minutes=idx),
                    outcome=CheckOutcome.UP.value,
                    latency_ms=40 + idx,
                )
            )
        db_session.commit()

        response = client.delete(
            f"/api/admin/monitoring/monitored-components/{component['id']}/check-results",
            params={"keep": 2},
        )
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["deleted_count"] == 3
        assert body["remaining_count"] == 2

        listed = client.get(
            f"/api/admin/monitoring/monitored-components/{component['id']}/check-results",
            params={"limit": 10},
        )
        assert listed.status_code == 200
        items = listed.json()["data"]["items"]
        assert len(items) == 2
        assert items[0]["latency_ms"] == 44
        assert items[1]["latency_ms"] == 43

    def test_purge_missing_component_404(self, client: TestClient, admin_headers: dict) -> None:
        missing = uuid4()
        response = client.delete(f"/api/admin/monitoring/monitored-components/{missing}/check-results")
        assert response.status_code == 404
        assert "not found" in response.json()["message"].lower()


class TestConnectionEventsEdges:
    def test_list_connection_events_empty(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="events-empty")
        component = _create_vpn_component(client, project_id=project["id"], slug="events-empty-vpn")
        response = client.get(
            f"/api/admin/monitoring/monitored-components/{component['id']}/connection-events"
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["items"] == []
        assert body["total"] == 0

    def test_list_connection_events_paginated(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        project = _create_project(client, slug="events-page")
        component = _create_vpn_component(client, project_id=project["id"], slug="events-page-vpn")
        now = datetime.now(UTC)
        for idx in range(5):
            db_session.add(
                ConnectionEvent(
                    monitored_component_id=component["id"],
                    occurred_at=now - timedelta(minutes=idx),
                    event_type=ConnectionEventType.TUNNEL_UP.value
                    if idx % 2 == 0
                    else ConnectionEventType.TUNNEL_DOWN.value,
                    outcome=CheckOutcome.UP.value if idx % 2 == 0 else CheckOutcome.DOWN.value,
                    message=f"event-{idx}",
                )
            )
        db_session.commit()

        page = client.get(
            f"/api/admin/monitoring/monitored-components/{component['id']}/connection-events",
            params={"offset": 1, "limit": 2},
        )
        assert page.status_code == 200, page.text
        body = page.json()["data"]
        assert body["total"] == 5
        assert body["offset"] == 1
        assert body["limit"] == 2
        assert len(body["items"]) == 2
        assert body["items"][0]["message"] == "event-1"
        assert body["items"][0]["event_label"]


class TestMonitoringSettingsValidationEdges:
    def test_get_settings_returns_defaults(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get("/api/admin/monitoring/settings")
        assert response.status_code == 200
        data = _data(response)
        assert data["default_poll_interval_seconds"] >= 10
        assert data["scheduler_interval_seconds"] >= 5
        assert "{bytes}" in data["default_speed_test_url_template"]

    def test_update_rejects_poll_interval_too_low(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_poll_interval_seconds": 5},
        )
        assert response.status_code == 422

    def test_update_rejects_scheduler_too_high(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"scheduler_interval_seconds": 9000},
        )
        assert response.status_code == 422

    def test_update_rejects_http_speed_template(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_speed_test_url_template": "http://cdn.example.com/x?b={bytes}"},
        )
        assert response.status_code == 422

    def test_update_rejects_empty_speed_template(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_speed_test_url_template": "   "},
        )
        assert response.status_code == 422

    def test_update_accepts_zero_speed_interval(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            "/api/admin/monitoring/settings",
            json={"default_speed_test_interval_seconds": 0},
        )
        assert response.status_code == 200, response.text
        assert _data(response)["default_speed_test_interval_seconds"] == 0
        # Restore a sane default for later tests in the same session.
        client.patch(
            "/api/admin/monitoring/settings",
            json={"default_speed_test_interval_seconds": 3600},
        )


class TestSpeedTestAdvisoryExotic:
    def test_advisory_with_custom_templates_not_cloudflare(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, slug="adv-custom")
        _create_vpn_component(
            client,
            project_id=project["id"],
            slug="adv-custom-vpn",
            speed_test_url_template="https://cdn.example.test/dl?n={bytes}",
            speed_test_interval_seconds=60,
        )
        response = client.get(
            "/api/admin/monitoring/speed-test-advisory",
            params={"project_id": project["id"]},
        )
        assert response.status_code == 200, response.text
        data = _data(response)
        assert data["active_vpn_service_count"] == 1
        assert data["uses_default_cloudflare_template"] is False
        assert data["estimated_speed_tests_per_minute"] > 0

    def test_advisory_warns_when_many_fast_vpn_services(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, slug="adv-warn")
        for idx in range(12):
            _create_vpn_component(
                client,
                project_id=project["id"],
                slug=f"adv-warn-vpn-{idx}",
                speed_test_interval_seconds=60,
            )
        response = client.get(
            "/api/admin/monitoring/speed-test-advisory",
            params={"project_id": project["id"]},
        )
        assert response.status_code == 200, response.text
        data = _data(response)
        assert data["active_vpn_service_count"] == 12
        assert data["warning"] is not None
        assert "HTTP requests" in data["warning"] or "per minute" in data["warning"].lower()


class TestManualCheckFailurePaths:
    def test_manual_check_rejected_for_persistent_openvpn(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, slug="manual-persist")
        component = _create_vpn_component(
            client,
            project_id=project["id"],
            slug="manual-persist-vpn",
            connection_mode="persistent",
        )
        response = client.post(f"/api/admin/monitoring/monitored-components/{component['id']}/check")
        assert response.status_code == 409
        assert "persistent" in response.json()["message"].lower()

    def test_manual_check_missing_component(self, client: TestClient, admin_headers: dict) -> None:
        response = client.post(f"/api/admin/monitoring/monitored-components/{uuid4()}/check")
        assert response.status_code == 404

    def test_manual_check_runner_failure_persisted(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, slug="manual-fail")
        component = _create_http_component(client, project_id=project["id"], slug="manual-fail-http")

        failed = CheckResult(
            id=uuid4(),
            monitored_component_id=component["id"],
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.ERROR.value,
            latency_ms=None,
            error_message="probe exploded",
            details={"error": "boom"},
        )

        with patch.object(HealthCheckRunner, "run_check", return_value=failed):
            response = client.post(
                f"/api/admin/monitoring/monitored-components/{component['id']}/check"
            )
        assert response.status_code == 200, response.text
        data = _data(response)
        assert data["outcome"] == "error"
        assert data["error_message"] == "probe exploded"


class TestRunDueChecksInactiveAndStagger:
    def test_is_due_skips_inactive_component(self) -> None:
        from unittest.mock import MagicMock

        from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings

        component = MonitoredComponent(
            id=uuid4(),
            project_id=uuid4(),
            component_kind_id=WEB_COMPONENT_KIND_ID,
            name="Inactive",
            slug="inactive",
            check_url="https://example.com",
            check_method="GET",
            check_type=CheckType.HTTP_STATUS.value,
            expected_status_code=200,
            timeout_seconds=10,
            is_active=False,
            last_checked_at=None,
        )
        runner = HealthCheckRunner(MagicMock())
        settings = MonitoringSettings(
            id=MONITORING_SETTINGS_ID,
            default_poll_interval_seconds=60,
            scheduler_interval_seconds=30,
            default_speed_test_url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE,
            default_speed_test_interval_seconds=3600,
        )
        assert runner.is_due(component, settings) is False

    def test_list_due_skips_inactive_project_and_component(self, db_session: Session) -> None:
        active_project = Project(name="Active", slug="due-active", description=None, is_active=True)
        inactive_project = Project(name="Inactive", slug="due-inactive", description=None, is_active=False)
        db_session.add_all([active_project, inactive_project])
        db_session.flush()

        active = MonitoredComponent(
            project_id=active_project.id,
            component_kind_id=WEB_COMPONENT_KIND_ID,
            name="Active HTTP",
            slug="due-active-http",
            check_url="https://example.com",
            check_method="GET",
            check_type=CheckType.HTTP_STATUS.value,
            expected_status_code=200,
            timeout_seconds=10,
            is_active=True,
            last_checked_at=None,
        )
        inactive_comp = MonitoredComponent(
            project_id=active_project.id,
            component_kind_id=WEB_COMPONENT_KIND_ID,
            name="Inactive HTTP",
            slug="due-inactive-http",
            check_url="https://example.com",
            check_method="GET",
            check_type=CheckType.HTTP_STATUS.value,
            expected_status_code=200,
            timeout_seconds=10,
            is_active=False,
            last_checked_at=None,
        )
        on_inactive_project = MonitoredComponent(
            project_id=inactive_project.id,
            component_kind_id=WEB_COMPONENT_KIND_ID,
            name="Hidden HTTP",
            slug="due-hidden-http",
            check_url="https://example.com",
            check_method="GET",
            check_type=CheckType.HTTP_STATUS.value,
            expected_status_code=200,
            timeout_seconds=10,
            is_active=True,
            last_checked_at=None,
        )
        db_session.add_all([active, inactive_comp, on_inactive_project])
        db_session.commit()

        due = HealthCheckRunner(db_session).list_due_components()
        due_ids = {component.id for component in due}
        assert active.id in due_ids
        assert inactive_comp.id not in due_ids
        assert on_inactive_project.id not in due_ids

    def test_run_due_checks_passes_staggered_speed_ids(self, db_session: Session) -> None:
        project = Project(name="Stagger edges", slug="stagger-edges", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()

        components = []
        for idx in range(2):
            component = MonitoredComponent(
                project_id=project.id,
                component_kind_id=OPENVPN_COMPONENT_KIND_ID,
                name=f"VPN {idx}",
                slug=f"stagger-edge-{idx}",
                check_url="https://ifconfig.me/ip",
                check_method="GET",
                check_type=CheckType.OPENVPN.value,
                check_config={"config_text": "client\ndev tun\nremote x 1194\n"},
                expected_status_code=200,
                timeout_seconds=60,
                connection_mode=ConnectionMode.EPHEMERAL.value,
                speed_test_enabled=True,
                speed_test_interval_seconds=3600,
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
                    outcome=CheckOutcome.UP.value,
                    details={"network": {"speed_test": {"ok": True, "mbps": 10.0, "measured_at": measured_at}}},
                )
            )
        db_session.commit()

        allowed_seen: list[set | None] = []

        def fake_run_check(self, component, *, speed_test_allowed_ids=None):
            allowed_seen.append(speed_test_allowed_ids)
            return CheckResult(
                monitored_component_id=component.id,
                checked_at=datetime.now(UTC),
                outcome=CheckOutcome.UP.value,
                latency_ms=50,
            )

        with (
            patch("app.services.monitoring_service.run_host_wan_speed_if_due"),
            patch.object(HealthCheckRunner, "run_check", fake_run_check),
        ):
            HealthCheckRunner(db_session).run_due_checks()

        assert len(allowed_seen) == 2
        assert all(isinstance(ids, set) for ids in allowed_seen)
        assert all(len(ids) <= 1 for ids in allowed_seen)  # type: ignore[arg-type]
        assert allowed_seen[0] == allowed_seen[1]
