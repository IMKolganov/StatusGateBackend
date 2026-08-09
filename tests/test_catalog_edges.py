"""Exotic / edge coverage for admin catalog APIs."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.monitored_component import MonitoredComponent
from app.models.project import Project


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


def _create_kind(client: TestClient, slug: str | None = None) -> dict:
    kind_slug = slug or f"kind-{uuid4().hex[:8]}"
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "Edge Kind", "slug": kind_slug, "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_project(client: TestClient, slug: str | None = None, *, is_active: bool = True) -> dict:
    project_slug = slug or f"proj-{uuid4().hex[:8]}"
    response = client.post(
        "/api/admin/projects",
        json={
            "name": "Edge Project",
            "slug": project_slug,
            "description": None,
            "is_active": is_active,
        },
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_component(
    client: TestClient,
    *,
    project_id: str,
    kind_id: str,
    slug: str = "backend",
) -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": kind_id,
            "name": "Backend API",
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


def _seeded_kind(client: TestClient, slug: str) -> dict:
    kinds = client.get("/api/admin/component-kinds")
    assert kinds.status_code == 200
    return next(item for item in _data(kinds)["items"] if item["slug"] == slug)


class TestProjectSlugAndLifecycleEdges:
    def test_project_slug_collision_returns_409(self, client: TestClient, admin_headers: dict) -> None:
        _create_project(client, slug="taken-slug")
        response = client.post(
            "/api/admin/projects",
            json={"name": "Other", "slug": "taken-slug", "description": None, "is_active": True},
        )
        assert response.status_code == 409
        assert "slug already exists" in _error_message(response).lower()

    def test_update_inactive_project_still_works(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="inactive-edit", is_active=False)
        assert project["is_active"] is False

        updated = client.patch(
            f"/api/admin/projects/{project['id']}",
            json={"name": "Still editable", "description": "revived metadata"},
        )
        assert updated.status_code == 200, updated.text
        body = _data(updated)
        assert body["name"] == "Still editable"
        assert body["description"] == "revived metadata"
        assert body["is_active"] is False

        reactivated = client.patch(
            f"/api/admin/projects/{project['id']}",
            json={"is_active": True},
        )
        assert reactivated.status_code == 200
        assert _data(reactivated)["is_active"] is True

    def test_update_project_slug_collision_returns_409(self, client: TestClient, admin_headers: dict) -> None:
        first = _create_project(client, slug="edge-first")
        _create_project(client, slug="edge-second")
        response = client.patch(
            f"/api/admin/projects/{first['id']}",
            json={"slug": "edge-second"},
        )
        assert response.status_code == 409
        assert "slug already exists" in _error_message(response).lower()

    def test_delete_project_cascades_components(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        project = _create_project(client, slug="cascade-edge")
        kind = _create_kind(client)
        first = _create_component(client, project_id=project["id"], kind_id=kind["id"], slug="one")
        second = _create_component(client, project_id=project["id"], kind_id=kind["id"], slug="two")

        delete = client.delete(f"/api/admin/projects/{project['id']}")
        assert delete.status_code == 200
        assert delete.json()["success"] is True

        assert db_session.get(Project, project["id"]) is None
        remaining = db_session.scalar(
            select(func.count()).select_from(MonitoredComponent).where(
                MonitoredComponent.id.in_([first["id"], second["id"]])
            )
        )
        assert remaining == 0


class TestCatalogListFiltersAndValidation:
    def test_list_monitored_components_filters_by_project_id(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        kind = _create_kind(client)
        project_a = _create_project(client, slug="filter-a")
        project_b = _create_project(client, slug="filter-b")
        _create_component(client, project_id=project_a["id"], kind_id=kind["id"], slug="svc-a")
        _create_component(client, project_id=project_b["id"], kind_id=kind["id"], slug="svc-b")

        filtered = client.get(
            "/api/admin/monitored-components",
            params={"project_id": project_a["id"]},
        )
        assert filtered.status_code == 200
        items = _data(filtered)["items"]
        assert len(items) == 1
        assert items[0]["slug"] == "svc-a"
        assert items[0]["project_id"] == project_a["id"]

        empty = client.get(
            "/api/admin/monitored-components",
            params={"project_id": str(uuid4())},
        )
        assert empty.status_code == 200
        assert _data(empty)["items"] == []
        assert _data(empty)["total"] == 0

    def test_missing_required_fields_rejected(self, client: TestClient, admin_headers: dict) -> None:
        project_missing = client.post("/api/admin/projects", json={"name": "No slug"})
        assert project_missing.status_code == 422

        kind_missing = client.post("/api/admin/component-kinds", json={"slug": "no-name"})
        assert kind_missing.status_code == 422

        project = _create_project(client, slug="req-fields")
        kind = _create_kind(client)
        component_missing = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "No slug component",
            },
        )
        assert component_missing.status_code == 422

        invalid_slug = client.post(
            "/api/admin/projects",
            json={"name": "Bad", "slug": "NOT_VALID", "is_active": True},
        )
        assert invalid_slug.status_code == 422


class TestVpnConfigEdges:
    def test_invalid_openvpn_config_rejected(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="ovpn-bad")
        openvpn_kind = _seeded_kind(client, "openvpn")

        missing_config = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "VPN missing config",
                "slug": "vpn-missing",
                "check_type": "openvpn",
            },
        )
        assert missing_config.status_code == 422

        too_short = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "VPN short config",
                "slug": "vpn-short",
                "check_type": "openvpn",
                "check_config": {"config_text": "client"},
            },
        )
        assert too_short.status_code == 422

        empty_text = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "VPN empty config",
                "slug": "vpn-empty",
                "check_type": "openvpn",
                "check_config": {"config_text": ""},
            },
        )
        assert empty_text.status_code == 422

    def test_invalid_xray_config_rejected(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="xray-bad")
        xray_kind = _seeded_kind(client, "xray")

        missing_config = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": xray_kind["id"],
                "name": "Xray missing config",
                "slug": "xray-missing",
                "check_type": "xray",
            },
        )
        assert missing_config.status_code == 422

        too_short = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": xray_kind["id"],
                "name": "Xray short config",
                "slug": "xray-short",
                "check_type": "xray",
                "check_config": {"config_text": "{}"},
            },
        )
        assert too_short.status_code == 422

        persistent = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": xray_kind["id"],
                "name": "Xray persistent",
                "slug": "xray-persistent",
                "check_type": "xray",
                "check_config": {
                    "config_text": '{"inbounds":[{"protocol":"socks","port":1080,"listen":"127.0.0.1"}]}',
                },
                "connection_mode": "persistent",
            },
        )
        assert persistent.status_code == 422


class TestComponentSlugAndReferenceEdges:
    def test_component_update_slug_collision_returns_409(
        self,
        client: TestClient,
        admin_headers: dict,
    ) -> None:
        project = _create_project(client, slug="comp-slug")
        kind = _create_kind(client)
        _create_component(client, project_id=project["id"], kind_id=kind["id"], slug="alpha")
        other = _create_component(client, project_id=project["id"], kind_id=kind["id"], slug="beta")

        response = client.patch(
            f"/api/admin/monitored-components/{other['id']}",
            json={"slug": "alpha"},
        )
        assert response.status_code == 409
        assert "slug already exists" in _error_message(response).lower()

    def test_kind_update_slug_collision_returns_409(self, client: TestClient, admin_headers: dict) -> None:
        first = _create_kind(client, slug="kind-alpha")
        _create_kind(client, slug="kind-beta")
        response = client.patch(
            f"/api/admin/component-kinds/{first['id']}",
            json={"slug": "kind-beta"},
        )
        assert response.status_code == 409

    def test_component_without_project_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        kind = _create_kind(client)
        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": str(uuid4()),
                "component_kind_id": kind["id"],
                "name": "Orphan",
                "slug": "orphan",
                "check_url": "https://example.com/health",
            },
        )
        assert response.status_code == 404
        assert "project not found" in _error_message(response).lower()

    def test_component_without_kind_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="missing-kind")
        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": str(uuid4()),
                "name": "No kind",
                "slug": "no-kind",
                "check_url": "https://example.com/health",
            },
        )
        assert response.status_code == 404
        assert "component kind not found" in _error_message(response).lower()

    def test_get_missing_component_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get(f"/api/admin/monitored-components/{uuid4()}")
        assert response.status_code == 404
