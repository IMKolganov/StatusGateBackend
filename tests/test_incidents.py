from datetime import UTC, datetime

from fastapi.testclient import TestClient


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

        history = client.get("/api/status/projects/history-demo/history")
        assert history.status_code == 200
        body = history.json()["data"]
        assert body["project_slug"] == "history-demo"
        assert len(body["days"]) >= 1
        assert body["days"][0]["entries"][0]["message"] == "All impacted services have now fully recovered."

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
