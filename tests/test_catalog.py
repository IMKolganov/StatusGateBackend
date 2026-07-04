from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome
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


def _create_kind(client: TestClient, slug: str = "api") -> dict:
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "API", "slug": slug, "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_project(client: TestClient, slug: str = "statusgate") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "StatusGate", "slug": slug, "description": "Main", "is_active": True},
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


class TestProjectsApi:
    def test_create_list_update_project(self, client: TestClient, admin_headers: dict) -> None:
        created = _create_project(client, slug="alpha")
        assert created["slug"] == "alpha"

        listing = client.get("/api/admin/projects")
        assert listing.status_code == 200
        items = _data(listing)["items"]
        assert len(items) == 1
        assert items[0]["slug"] == "alpha"

        updated = client.patch(
            f"/api/admin/projects/{created['id']}",
            json={"name": "Alpha renamed", "is_active": False},
        )
        assert updated.status_code == 200
        body = _data(updated)
        assert body["name"] == "Alpha renamed"
        assert body["is_active"] is False

    def test_duplicate_project_slug_rejected(self, client: TestClient, admin_headers: dict) -> None:
        _create_project(client, slug="duplicate-me")
        response = client.post(
            "/api/admin/projects",
            json={"name": "Other", "slug": "duplicate-me", "description": None, "is_active": True},
        )
        assert response.status_code == 409
        assert "slug already exists" in _error_message(response).lower()

    def test_update_project_slug_conflict_rejected(self, client: TestClient, admin_headers: dict) -> None:
        first = _create_project(client, slug="first-project")
        _create_project(client, slug="second-project")
        response = client.patch(
            f"/api/admin/projects/{first['id']}",
            json={"slug": "second-project"},
        )
        assert response.status_code == 409

    def test_delete_project_cascades_components_and_check_results(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        project = _create_project(client, slug="cascade-project")
        kind = _create_kind(client)
        component = _create_component(client, project_id=project["id"], kind_id=kind["id"])

        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                outcome=CheckOutcome.UP.value,
                latency_ms=42,
                http_status_code=200,
            )
        )
        db_session.commit()

        delete = client.delete(f"/api/admin/projects/{project['id']}")
        assert delete.status_code == 200
        assert delete.json()["success"] is True

        assert db_session.get(Project, project["id"]) is None
        assert (
            db_session.scalar(
                select(func.count()).select_from(MonitoredComponent).where(
                    MonitoredComponent.project_id == project["id"]
                )
            )
            == 0
        )
        assert (
            db_session.scalar(
                select(func.count()).select_from(CheckResult).where(
                    CheckResult.monitored_component_id == component["id"]
                )
            )
            == 0
        )

    def test_delete_missing_project_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.delete(f"/api/admin/projects/{uuid4()}")
        assert response.status_code == 404


class TestComponentKindsApi:
    def test_default_component_kinds_seeded(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get("/api/admin/component-kinds")
        assert response.status_code == 200
        slugs = {item["slug"] for item in _data(response)["items"]}
        assert {"web", "openvpn", "xray"}.issubset(slugs)

    def test_duplicate_kind_slug_rejected(self, client: TestClient, admin_headers: dict) -> None:
        response = client.post(
            "/api/admin/component-kinds",
            json={"name": "Web duplicate", "slug": "web", "description": None},
        )
        assert response.status_code == 409

    def test_openvpn_component_requires_config(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="vpn-project")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "VPN without config",
                "slug": "vpn-no-config",
                "check_type": "openvpn",
                "check_url": "https://ifconfig.me/ip",
            },
        )
        assert response.status_code == 422

    def test_openvpn_component_created_with_config(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="vpn-create")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Norway OpenVPN",
                "slug": "norway-openvpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
                "timeout_seconds": 30,
            },
        )
        assert response.status_code == 201, response.text
        body = _data(response)
        assert body["check_type"] == "openvpn"
        assert body["check_url"] == "https://ifconfig.me/ip"
        assert body["check_config"]["config_text"].startswith("client")

    def test_openvpn_component_created_with_persistent_mode(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="vpn-persistent")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Persistent VPN",
                "slug": "persistent-vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "check_url": "https://www.google.com/generate_204",
                "timeout_seconds": 30,
            },
        )
        assert response.status_code == 201, response.text
        body = _data(response)
        assert body["connection_mode"] == "persistent"
        assert body["check_url"] == "https://www.google.com/generate_204"

    def test_xray_component_persistent_mode_rejected(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="xray-persistent")
        kinds = client.get("/api/admin/component-kinds")
        xray_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "xray")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": xray_kind["id"],
                "name": "Persistent Xray",
                "slug": "persistent-xray",
                "check_type": "xray",
                "check_config": {
                    "config_text": '{"inbounds":[{"protocol":"socks","port":1080,"listen":"127.0.0.1"}]}',
                },
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        assert response.status_code == 422

    def test_xray_component_created_with_config(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="xray-create")
        kinds = client.get("/api/admin/component-kinds")
        xray_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "xray")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": xray_kind["id"],
                "name": "Norway Xray",
                "slug": "norway-xray",
                "check_type": "xray",
                "check_config": {
                    "config_text": '{"inbounds":[{"protocol":"socks","port":1080,"listen":"127.0.0.1"}]}',
                },
                "check_url": "https://ifconfig.me/ip",
                "timeout_seconds": 30,
            },
        )
        assert response.status_code == 201, response.text
        body = _data(response)
        assert body["check_type"] == "xray"
        assert "inbounds" in body["check_config"]["config_text"]

    def test_kind_in_use_cannot_be_deleted(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="kind-lock")
        kind = _create_kind(client, slug="locked-kind")
        _create_component(client, project_id=project["id"], kind_id=kind["id"])

        response = client.delete(f"/api/admin/component-kinds/{kind['id']}")
        assert response.status_code == 409


class TestMonitoredComponentsApi:
    def test_duplicate_slug_within_project_rejected(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="dup-components")
        kind = _create_kind(client, slug="service")
        _create_component(client, project_id=project["id"], kind_id=kind["id"], slug="api")

        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": kind["id"],
                "name": "Duplicate slug",
                "slug": "api",
                "check_url": "https://example.com/health",
            },
        )
        assert response.status_code == 409

    def test_same_slug_allowed_in_different_projects(self, client: TestClient, admin_headers: dict) -> None:
        kind = _create_kind(client, slug="shared-kind")
        project_a = _create_project(client, slug="project-a")
        project_b = _create_project(client, slug="project-b")

        first = _create_component(client, project_id=project_a["id"], kind_id=kind["id"], slug="api")
        second = _create_component(client, project_id=project_b["id"], kind_id=kind["id"], slug="api")

        assert first["slug"] == second["slug"] == "api"
        assert first["project_id"] != second["project_id"]

    def test_component_requires_existing_project_and_kind(self, client: TestClient, admin_headers: dict) -> None:
        kind = _create_kind(client, slug="orphan-kind")
        response = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": str(uuid4()),
                "component_kind_id": kind["id"],
                "name": "Broken",
                "slug": "broken",
                "check_url": "https://example.com/health",
            },
        )
        assert response.status_code == 404
