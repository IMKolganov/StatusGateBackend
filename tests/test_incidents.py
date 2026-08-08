from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome


class TestIncidentHistory:
    def test_create_and_view_public_history(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "History Demo", "slug": "history-demo", "description": None, "is_active": True},
        ).json()["data"]

        posted_at = datetime(2026, 6, 16, 13, 48, tzinfo=UTC).isoformat().replace("+00:00", "Z")
        create = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": 'Codex "Selected Model is at Capacity" Error',
                "message": "All impacted services have now fully recovered.",
                "status": "resolved",
                "posted_at": posted_at,
            },
        )
        assert create.status_code == 201, create.text
        incident = create.json()["data"]
        assert incident["title"].startswith("Codex")
        assert len(incident["updates"]) == 1
        assert incident["starts_at"].startswith("2026-06-16T13:48")

        history = client.get("/api/status/projects/history-demo/history")
        assert history.status_code == 200
        body = history.json()["data"]
        assert body["project_slug"] == "history-demo"
        assert len(body["days"]) >= 1
        entry = body["days"][0]["entries"][0]
        assert entry["message"] == "All impacted services have now fully recovered."
        assert entry["starts_at"].startswith("2026-06-16T13:48")

        update = client.post(
            f"/api/admin/incidents/{incident['id']}/updates",
            json={
                "message": "We are investigating the issue for the listed services.",
                "status": "investigating",
                "posted_at": datetime(2026, 6, 16, 10, 16, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            },
        )
        assert update.status_code == 201

        history2 = client.get("/api/status/projects/history-demo/history")
        day_entries = history2.json()["data"]["days"][0]["entries"]
        assert len(day_entries) == 2

    def test_patch_incident_title_service_and_range(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        kind = client.post(
            "/api/admin/component-kinds",
            json={"name": "APIs", "slug": f"apis-{uuid4().hex[:8]}", "description": None},
        ).json()["data"]
        project = client.post(
            "/api/admin/projects",
            json={"name": "Edit Demo", "slug": f"edit-demo-{uuid4().hex[:8]}", "description": None, "is_active": True},
        ).json()["data"]
        component = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Helsinki OpenVPN",
                "slug": "helsinki-openvpn",
                "check_url": "https://example.com/health",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "is_active": True,
            },
        ).json()["data"]

        create = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Original title",
                "message": "Looking into it.",
                "status": "investigating",
            },
        )
        assert create.status_code == 201, create.text
        incident = create.json()["data"]
        update_id = incident["updates"][0]["id"]

        starts = datetime(2026, 8, 4, 20, 20, tzinfo=UTC).isoformat().replace("+00:00", "Z")
        ends = datetime(2026, 8, 7, 11, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
        patched_incident = client.patch(
            f"/api/admin/incidents/{incident['id']}",
            json={
                "title": "Helsinki OpenVPN unavailable in Russia",
                "monitored_component_id": component["id"],
                "starts_at": starts,
                "ends_at": ends,
            },
        )
        assert patched_incident.status_code == 200, patched_incident.text
        body = patched_incident.json()["data"]
        assert body["title"] == "Helsinki OpenVPN unavailable in Russia"
        assert body["monitored_component_id"] == component["id"]
        assert body["service_name"] == "Helsinki OpenVPN"
        assert body["starts_at"].startswith("2026-08-04T20:20")
        assert body["ends_at"].startswith("2026-08-07T11:24")

        patched_update = client.patch(
            f"/api/admin/incident-updates/{update_id}",
            json={
                "message": "Service restored.",
                "status": "resolved",
                "posted_at": ends,
            },
        )
        assert patched_update.status_code == 200, patched_update.text
        assert patched_update.json()["data"]["status"] == "resolved"

    def test_system_status_filters_incidents_by_service_and_range(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = client.post(
            "/api/admin/component-kinds",
            json={"name": "VPN", "slug": f"vpn-{uuid4().hex[:8]}", "description": None},
        ).json()["data"]
        project = client.post(
            "/api/admin/projects",
            json={
                "name": "Filter Demo",
                "slug": f"filter-demo-{uuid4().hex[:8]}",
                "description": None,
                "is_active": True,
            },
        ).json()["data"]
        helsinki = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Helsinki",
                "slug": "helsinki",
                "check_url": "https://example.com/h",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "is_active": True,
            },
        ).json()["data"]
        norway = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Norway",
                "slug": "norway",
                "check_url": "https://example.com/n",
                "check_method": "GET",
                "expected_status_code": 200,
                "timeout_seconds": 10,
                "is_active": True,
            },
        ).json()["data"]

        for component_id, day in (
            (helsinki["id"], datetime(2026, 8, 5, 12, 0, tzinfo=UTC)),
            (norway["id"], datetime(2026, 8, 5, 12, 0, tzinfo=UTC)),
        ):
            db_session.add(
                CheckResult(
                    monitored_component_id=component_id,
                    checked_at=day,
                    outcome=CheckOutcome.UP.value,
                    latency_ms=40,
                )
            )
        db_session.commit()

        create = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Helsinki block",
                "message": "Blocked in RF",
                "status": "resolved",
                "monitored_component_id": helsinki["id"],
                "starts_at": datetime(2026, 8, 4, 20, 20, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
                "ends_at": datetime(2026, 8, 7, 11, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            },
        )
        assert create.status_code == 201, create.text

        response = client.get(
            f"/api/status/projects/{project['slug']}/system-status",
            params={"end": "2026-08-08", "days": 7},
        )
        assert response.status_code == 200, response.text
        services = {
            service["slug"]: service
            for group in response.json()["data"]["groups"]
            for service in group["services"]
        }

        helsinki_days = {day["date"]: day for day in services["helsinki"]["days"]}
        norway_days = {day["date"]: day for day in services["norway"]["days"]}

        assert len(helsinki_days["2026-08-05"]["incidents"]) == 1
        assert helsinki_days["2026-08-05"]["incidents"][0]["title"] == "Helsinki block"
        assert helsinki_days["2026-08-05"]["incidents"][0]["service_name"] == "Helsinki"
        assert helsinki_days["2026-08-03"]["incidents"] == []
        assert norway_days["2026-08-05"]["incidents"] == []

    def test_inactive_project_history_hidden(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "Hidden", "slug": "hidden-project", "description": None, "is_active": False},
        ).json()["data"]
        client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={"title": "Secret", "message": "Should not show", "status": "update"},
        )
        response = client.get("/api/status/projects/hidden-project/history")
        assert response.status_code == 404
