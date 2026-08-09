"""Public status API edges and uncovered private helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.component_kind import WEB_COMPONENT_KIND_ID
from app.models.enums import CheckOutcome
from app.models.incident import Incident
from app.services.public_status_service import (
    _as_float,
    _as_int,
    _build_tunnel_latest_diagnostics,
    _date_range,
    _format_range_label,
    _incident_matches_scope,
    _incident_overlaps_day,
    _project_day_statuses,
    _public_day_incident,
    _status_label,
)
from app.services.uptime_stats import DayCheckStats, empty_day_stats


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_project(client: TestClient, slug: str, *, is_active: bool = True) -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": slug, "slug": slug, "description": None, "is_active": is_active},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_http_component(client: TestClient, *, project_id: str, slug: str) -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": str(WEB_COMPONENT_KIND_ID),
            "name": slug,
            "slug": slug,
            "check_url": "https://example.com/health",
            "check_method": "GET",
            "expected_status_code": 200,
            "timeout_seconds": 10,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return _data(response)


class TestPublicProjectStatusEdges:
    def test_empty_project_status(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, "empty-status")
        response = client.get(f"/api/status/projects/{project['slug']}")
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["slug"] == "empty-status"
        assert body["services"] == []

    def test_unknown_slug_404(self, client: TestClient) -> None:
        response = client.get("/api/status/projects/does-not-exist-xyz")
        assert response.status_code == 404
        assert "not found" in response.json()["message"].lower()

    def test_inactive_project_status_404(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, "inactive-status", is_active=False)
        response = client.get(f"/api/status/projects/{project['slug']}")
        assert response.status_code == 404


class TestPublicSystemStatusNoChecks:
    def test_system_status_unknown_days_without_checks(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, "no-checks-timeline")
        _create_http_component(client, project_id=project["id"], slug="lonely-api")

        response = client.get(
            "/api/status/projects/no-checks-timeline/system-status",
            params={"end": "2026-06-20", "days": 7},
        )
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["project_slug"] == "no-checks-timeline"
        assert len(body["groups"]) == 1
        group = body["groups"][0]
        assert group["component_count"] == 1
        assert len(group["days"]) == 7
        assert all(day["status"] == "no_data" for day in group["days"])
        assert all(day["check_count"] == 0 for day in group["days"])
        service = group["services"][0]
        assert all(day["status"] == "no_data" for day in service["days"])
        assert service["uptime_percent"] is None


class TestPublicTunnelMetricsMissing:
    def test_tunnel_metrics_missing_service_404(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, "tunnel-missing")
        _create_http_component(client, project_id=project["id"], slug="present-svc")
        response = client.get(
            "/api/status/projects/tunnel-missing/services/no-such-service/tunnel-metrics"
        )
        assert response.status_code == 404
        assert "service" in response.json()["message"].lower()

    def test_tunnel_metrics_missing_project_404(self, client: TestClient) -> None:
        response = client.get(
            "/api/status/projects/no-project/services/anything/tunnel-metrics"
        )
        assert response.status_code == 404


class TestPublicHistoryEmpty:
    def test_history_empty_project(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, "history-empty")
        response = client.get(f"/api/status/projects/{project['slug']}/history")
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["project_slug"] == "history-empty"
        assert body["days"] == []


class TestPublicProjectsPagination:
    def test_list_public_projects_respects_limit(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        for idx in range(3):
            project = _create_project(client, f"page-proj-{idx}")
            component = _create_http_component(
                client,
                project_id=project["id"],
                slug=f"page-svc-{idx}",
            )
            db_session.add(
                CheckResult(
                    monitored_component_id=component["id"],
                    checked_at=datetime.now(UTC),
                    outcome=CheckOutcome.UP.value,
                    latency_ms=10,
                )
            )
        db_session.commit()

        limited = client.get("/api/status/projects", params={"limit": 1})
        assert limited.status_code == 200, limited.text
        items = limited.json()["data"]
        assert len(items) == 1

        broader = client.get("/api/status/projects", params={"limit": 100})
        assert broader.status_code == 200
        assert len(broader.json()["data"]) >= 3

    def test_list_public_projects_rejects_invalid_limit(self, client: TestClient) -> None:
        response = client.get("/api/status/projects", params={"limit": 0})
        assert response.status_code == 422


class TestPublicStatusPrivateHelpers:
    def test_as_float_and_as_int_edges(self) -> None:
        assert _as_float(None) is None
        assert _as_float("12.5") == 12.5
        assert _as_float("nope") is None
        assert _as_float(object()) is None
        assert _as_int(None) is None
        assert _as_int("7") == 7
        assert _as_int("x") is None
        assert _as_int(3.9) == 3

    def test_date_range_and_labels(self) -> None:
        days = _date_range(date(2026, 1, 30), date(2026, 2, 1))
        assert days == [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 1)]
        assert _format_range_label(date(2026, 6, 1), date(2026, 6, 30)) == "Jun 2026"
        assert _format_range_label(date(2026, 1, 1), date(2026, 3, 1)) == "Jan–Mar 2026"
        assert _format_range_label(date(2025, 12, 1), date(2026, 1, 31)) == "Dec 2025–Jan 2026"

    def test_status_label(self) -> None:
        assert _status_label(CheckOutcome.UP.value) == "Operational"
        assert _status_label(CheckOutcome.DOWN.value) == "Outage"
        assert _status_label("custom") == "Custom"

    def test_incident_scope_and_overlap(self) -> None:
        component_id = uuid4()
        other_id = uuid4()
        incident = Incident(
            id=uuid4(),
            project_id=uuid4(),
            title="Scoped",
            starts_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            ends_at=datetime(2026, 6, 20, 18, 0, tzinfo=UTC),
            monitored_component_id=component_id,
        )
        project_wide = Incident(
            id=uuid4(),
            project_id=uuid4(),
            title="Global",
            starts_at=datetime(2026, 6, 20, 0, 0, tzinfo=UTC),
            ends_at=None,
            monitored_component_id=None,
        )

        assert _incident_matches_scope(incident, component_id=component_id) is True
        assert _incident_matches_scope(incident, component_id=other_id) is False
        assert _incident_matches_scope(incident, component_ids={component_id}) is True
        assert _incident_matches_scope(incident, component_ids={other_id}) is False
        assert _incident_matches_scope(project_wide, component_id=other_id) is True

        now = datetime(2026, 6, 21, 0, 0, tzinfo=UTC)
        assert _incident_overlaps_day(incident, date(2026, 6, 20), now=now) is True
        assert _incident_overlaps_day(incident, date(2026, 6, 21), now=now) is False
        assert _incident_overlaps_day(project_wide, date(2026, 6, 21), now=now) is True

    def test_public_day_incident_uses_latest_update_before_day_end(self) -> None:
        early = SimpleNamespace(
            message="early",
            status="investigating",
            posted_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
        late = SimpleNamespace(
            message="late",
            status="resolved",
            posted_at=datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
        )
        after = SimpleNamespace(
            message="next day",
            status="resolved",
            posted_at=datetime(2026, 6, 21, 1, 0, tzinfo=UTC),
        )
        incident = SimpleNamespace(
            title="Day incident",
            starts_at=datetime(2026, 6, 20, 8, 0, tzinfo=UTC),
            ends_at=None,
            updates=[after, early, late],
            monitored_component=None,
        )

        day = _public_day_incident(incident, date(2026, 6, 20))  # type: ignore[arg-type]
        assert day.message == "late"
        assert day.status == "resolved"

    def test_project_day_statuses_merges_worst(self) -> None:
        first = uuid4()
        second = uuid4()
        day = date(2026, 6, 20)
        stats = {
            (first, day): DayCheckStats.from_outcomes([CheckOutcome.UP.value]),
            (second, day): DayCheckStats.from_outcomes([CheckOutcome.DOWN.value] * 5),
        }
        statuses = _project_day_statuses([first, second], [day], stats)
        assert statuses[day] == "outage"
        empty = _project_day_statuses([], [day], {})
        assert empty[day] == "no_data"
        assert empty_day_stats().total == 0

    def test_build_tunnel_latest_diagnostics_empty_and_without_summary(self) -> None:
        assert _build_tunnel_latest_diagnostics([], []) is None

        row = SimpleNamespace(
            checked_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            outcome=CheckOutcome.UP.value,
            details=None,
        )
        point = SimpleNamespace(
            outcome="up",
            download_mbps=10.0,
            download_cached=False,
        )
        diagnostics = _build_tunnel_latest_diagnostics([row], [point])  # type: ignore[arg-type]
        assert diagnostics is not None
        assert diagnostics.outcome == "up"
        assert diagnostics.fresh_speed_tests_in_window == 1
        assert diagnostics.uptime_percent == 100.0
        assert diagnostics.exit_ip is None
