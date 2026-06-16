from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_kind(client: TestClient, slug: str = "api") -> dict:
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "APIs", "slug": slug, "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_project(client: TestClient, slug: str = "timeline-demo") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Timeline Demo", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_component(client: TestClient, *, project_id: str, kind_id: str, slug: str, name: str) -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": kind_id,
            "name": name,
            "slug": slug,
            "description": None,
            "environment": "prod",
            "check_url": "https://example.com/health",
            "check_method": "GET",
            "expected_status_code": 200,
            "timeout_seconds": 10,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return _data(response)


class TestPublicSystemStatus:
    def test_system_status_timeline(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client)
        project = _create_project(client)
        component = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="backend",
            name="Backend API",
        )

        good_day = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        bad_day = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=good_day,
                outcome=CheckOutcome.UP.value,
                latency_ms=42,
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=bad_day,
                outcome=CheckOutcome.DEGRADED.value,
                latency_ms=120,
            )
        )
        db_session.commit()

        client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Partial outage",
                "message": "We are investigating degraded performance.",
                "status": "investigating",
                "posted_at": bad_day.isoformat().replace("+00:00", "Z"),
            },
        )

        response = client.get(
            "/api/status/projects/timeline-demo/system-status",
            params={"end": "2026-06-16", "days": 7},
        )
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["project_slug"] == "timeline-demo"
        assert body["range_label"] == "Jun 2026"
        assert len(body["groups"]) == 1
        group = body["groups"][0]
        assert group["name"] == "APIs"
        assert group["component_count"] == 1
        assert len(group["days"]) == 7

        day_by_date = {day["date"]: day for day in group["days"]}
        assert day_by_date["2026-06-15"]["status"] == "operational"
        assert day_by_date["2026-06-15"]["tooltip"] == "No incidents"
        assert day_by_date["2026-06-16"]["status"] == "degraded"
        assert day_by_date["2026-06-16"]["tooltip"] == "1 incident"
        assert len(day_by_date["2026-06-16"]["incidents"]) == 1

        assert len(body["active_alerts"]) >= 1

    def test_inactive_project_system_status_hidden(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "Hidden", "slug": "hidden-timeline", "description": None, "is_active": False},
        ).json()["data"]
        assert project["slug"] == "hidden-timeline"

        response = client.get("/api/status/projects/hidden-timeline/system-status")
        assert response.status_code == 404
