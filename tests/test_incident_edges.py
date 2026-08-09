"""Exotic / edge coverage for admin incident APIs and public surfaces."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _create_project(client: TestClient, slug: str | None = None) -> dict:
    project_slug = slug or f"inc-{uuid4().hex[:8]}"
    response = client.post(
        "/api/admin/projects",
        json={"name": "Incident Edge", "slug": project_slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_kind(client: TestClient) -> dict:
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "Edge Service", "slug": f"svc-{uuid4().hex[:8]}", "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_component(client: TestClient, *, project_id: str, kind_id: str, slug: str = "edge-svc") -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": kind_id,
            "name": "Edge Service",
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


def _create_incident(
    client: TestClient,
    *,
    project_id: str,
    title: str = "Edge incident",
    message: str = "Investigating",
    status: str = "investigating",
    monitored_component_id: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> dict:
    payload: dict = {
        "title": title,
        "message": message,
        "status": status,
    }
    if monitored_component_id is not None:
        payload["monitored_component_id"] = monitored_component_id
    if starts_at is not None:
        payload["starts_at"] = starts_at
    if ends_at is not None:
        payload["ends_at"] = ends_at
    response = client.post(f"/api/admin/projects/{project_id}/incidents", json=payload)
    assert response.status_code == 201, response.text
    return _data(response)


class TestIncidentNotFoundEdges:
    def test_bad_project_id_list_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get(f"/api/admin/projects/{uuid4()}/incidents")
        assert response.status_code == 404
        assert "project not found" in _error_message(response).lower()

    def test_bad_project_id_create_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.post(
            f"/api/admin/projects/{uuid4()}/incidents",
            json={"title": "Missing project", "message": "Nope", "status": "investigating"},
        )
        assert response.status_code == 404

    def test_bad_incident_id_patch_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.patch(
            f"/api/admin/incidents/{uuid4()}",
            json={"title": "Ghost"},
        )
        assert response.status_code == 404
        assert "incident not found" in _error_message(response).lower()

    def test_bad_incident_id_add_update_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.post(
            f"/api/admin/incidents/{uuid4()}/updates",
            json={"message": "Still missing", "status": "update"},
        )
        assert response.status_code == 404

    def test_bad_incident_id_delete_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.delete(f"/api/admin/incidents/{uuid4()}")
        assert response.status_code == 404

    def test_bad_update_id_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        patch = client.patch(
            f"/api/admin/incident-updates/{uuid4()}",
            json={"message": "Ghost update"},
        )
        assert patch.status_code == 404
        assert "incident update not found" in _error_message(patch).lower()

        delete = client.delete(f"/api/admin/incident-updates/{uuid4()}")
        assert delete.status_code == 404


class TestIncidentMessageAndStatusEdges:
    def test_empty_message_rejected_on_create_and_update(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client)
        create = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={"title": "Empty message", "message": "", "status": "investigating"},
        )
        assert create.status_code == 422

        incident = _create_incident(client, project_id=project["id"])
        update_id = incident["updates"][0]["id"]

        add = client.post(
            f"/api/admin/incidents/{incident['id']}/updates",
            json={"message": "", "status": "update"},
        )
        assert add.status_code == 422

        patch = client.patch(
            f"/api/admin/incident-updates/{update_id}",
            json={"message": ""},
        )
        assert patch.status_code == 422

    def test_patch_status_transitions(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client)
        incident = _create_incident(
            client,
            project_id=project["id"],
            status="investigating",
            message="Looking into it",
        )
        update_id = incident["updates"][0]["id"]

        for next_status in ("identified", "monitoring", "resolved"):
            patched = client.patch(
                f"/api/admin/incident-updates/{update_id}",
                json={"status": next_status, "message": f"Now {next_status}"},
            )
            assert patched.status_code == 200, patched.text
            body = _data(patched)
            assert body["status"] == next_status
            assert body["message"] == f"Now {next_status}"

        invalid = client.patch(
            f"/api/admin/incident-updates/{update_id}",
            json={"status": "not-a-status"},
        )
        assert invalid.status_code == 422

        follow_up = client.post(
            f"/api/admin/incidents/{incident['id']}/updates",
            json={"message": "Post-mortem note", "status": "update"},
        )
        assert follow_up.status_code == 201
        assert _data(follow_up)["status"] == "update"

    def test_delete_update(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client)
        incident = _create_incident(client, project_id=project["id"], message="Initial")
        second = client.post(
            f"/api/admin/incidents/{incident['id']}/updates",
            json={"message": "Second update", "status": "identified"},
        )
        assert second.status_code == 201
        update_id = _data(second)["id"]

        deleted = client.delete(f"/api/admin/incident-updates/{update_id}")
        assert deleted.status_code == 200
        assert deleted.json()["success"] is True

        listing = client.get(f"/api/admin/projects/{project['id']}/incidents")
        assert listing.status_code == 200
        items = _data(listing)
        assert len(items) == 1
        messages = {entry["message"] for entry in items[0]["updates"]}
        assert "Second update" not in messages
        assert "Initial" in messages


class TestIncidentDateRangeAndListEdges:
    def test_starts_at_after_ends_at_rejected_on_create(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client)
        starts = _iso(datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
        ends = _iso(datetime(2026, 8, 4, 12, 0, tzinfo=UTC))

        both_set = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Bad range",
                "message": "ends before starts",
                "status": "resolved",
                "starts_at": starts,
                "ends_at": ends,
            },
        )
        assert both_set.status_code == 422

        # Schema allows ends_at alone; service rejects once starts_at defaults to now/posted_at
        # when ends_at is clearly in the past relative to posted_at/starts default.
        far_past_end = client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Past end only",
                "message": "ends_at before default starts",
                "status": "resolved",
                "posted_at": starts,
                "ends_at": ends,
            },
        )
        assert far_past_end.status_code == 400
        assert "ends_at" in _error_message(far_past_end).lower()

    def test_starts_at_after_ends_at_rejected_on_patch(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client)
        incident = _create_incident(
            client,
            project_id=project["id"],
            starts_at=_iso(datetime(2026, 8, 4, 10, 0, tzinfo=UTC)),
            ends_at=_iso(datetime(2026, 8, 7, 10, 0, tzinfo=UTC)),
        )

        both_set = client.patch(
            f"/api/admin/incidents/{incident['id']}",
            json={
                "starts_at": _iso(datetime(2026, 8, 9, 10, 0, tzinfo=UTC)),
                "ends_at": _iso(datetime(2026, 8, 5, 10, 0, tzinfo=UTC)),
            },
        )
        assert both_set.status_code == 422

        ends_before_existing_start = client.patch(
            f"/api/admin/incidents/{incident['id']}",
            json={"ends_at": _iso(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))},
        )
        assert ends_before_existing_start.status_code == 400
        assert "ends_at" in _error_message(ends_before_existing_start).lower()

    def test_list_empty_project_returns_empty_list(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="empty-incidents")
        response = client.get(f"/api/admin/projects/{project['id']}/incidents")
        assert response.status_code == 200
        assert _data(response) == []


class TestComponentScopedIncidentSurfaces:
    def test_component_scoped_incident_on_system_status_and_history(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client)
        project = _create_project(client, slug=f"scoped-{uuid4().hex[:8]}")
        primary = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="primary",
        )
        other = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="other",
        )

        day = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        for component_id in (primary["id"], other["id"]):
            db_session.add(
                CheckResult(
                    monitored_component_id=component_id,
                    checked_at=day,
                    outcome=CheckOutcome.UP.value,
                    latency_ms=25,
                )
            )
        db_session.commit()

        starts = _iso(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
        ends = _iso(datetime(2026, 8, 6, 12, 0, tzinfo=UTC))
        incident = _create_incident(
            client,
            project_id=project["id"],
            title="Primary outage",
            message="Primary service degraded",
            status="resolved",
            monitored_component_id=primary["id"],
            starts_at=starts,
            ends_at=ends,
        )
        assert incident["service_slug"] == "primary"
        assert incident["monitored_component_id"] == primary["id"]

        status_response = client.get(
            f"/api/status/projects/{project['slug']}/system-status",
            params={"end": "2026-08-08", "days": 7},
        )
        assert status_response.status_code == 200, status_response.text
        services = {
            service["slug"]: service
            for group in _data(status_response)["groups"]
            for service in group["services"]
        }
        primary_days = {day_row["date"]: day_row for day_row in services["primary"]["days"]}
        other_days = {day_row["date"]: day_row for day_row in services["other"]["days"]}
        assert len(primary_days["2026-08-05"]["incidents"]) == 1
        assert primary_days["2026-08-05"]["incidents"][0]["title"] == "Primary outage"
        assert primary_days["2026-08-05"]["incidents"][0]["service_name"] == "Edge Service"
        assert other_days["2026-08-05"]["incidents"] == []

        history = client.get(f"/api/status/projects/{project['slug']}/history")
        assert history.status_code == 200
        entries = [
            entry
            for day_row in _data(history)["days"]
            for entry in day_row["entries"]
        ]
        assert len(entries) == 1
        assert entries[0]["service_slug"] == "primary"
        assert entries[0]["service_name"] == "Edge Service"
        assert entries[0]["title"] == "Primary outage"
        assert entries[0]["message"] == "Primary service degraded"

    def test_component_from_other_project_rejected(self, client: TestClient, admin_headers: dict) -> None:
        kind = _create_kind(client)
        project_a = _create_project(client)
        project_b = _create_project(client)
        foreign = _create_component(client, project_id=project_b["id"], kind_id=kind["id"], slug="foreign")

        response = client.post(
            f"/api/admin/projects/{project_a['id']}/incidents",
            json={
                "title": "Wrong project component",
                "message": "Should fail",
                "status": "investigating",
                "monitored_component_id": foreign["id"],
            },
        )
        assert response.status_code == 400
        assert "service not found" in _error_message(response).lower()
