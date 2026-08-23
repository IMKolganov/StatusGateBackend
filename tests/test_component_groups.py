"""Component groups: admin CRUD and public nesting."""

from fastapi.testclient import TestClient

from app.models.component_kind import WEB_COMPONENT_KIND_ID


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_project(client: TestClient, *, slug: str = "groups-proj") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Groups project", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_http_component(client: TestClient, *, project_id: str, slug: str, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "component_kind_id": str(WEB_COMPONENT_KIND_ID),
        "name": slug,
        "slug": slug,
        "check_url": "https://example.com/health",
        "check_type": "http_status",
        "is_active": True,
        **extra,
    }
    response = client.post("/api/admin/monitored-components", json=payload)
    assert response.status_code == 201, response.text
    return _data(response)


class TestComponentGroups:
    def test_group_crud_and_assign(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="grp-crud")
        other = _create_project(client, slug="grp-other")

        created = client.post(
            "/api/admin/component-groups",
            json={
                "project_id": project["id"],
                "name": "Server 1",
                "slug": "server-1",
                "sort_order": 1,
                "is_active": True,
            },
        )
        assert created.status_code == 201, created.text
        group = _data(created)
        assert group["name"] == "Server 1"

        listed = client.get("/api/admin/component-groups", params={"project_id": project["id"]})
        assert listed.status_code == 200
        assert any(item["id"] == group["id"] for item in _data(listed)["items"])

        component = _create_http_component(
            client,
            project_id=project["id"],
            slug="api-1",
            group_id=group["id"],
            sort_order=2,
        )
        assert component["group_id"] == group["id"]
        assert component["group_name"] == "Server 1"
        assert component["sort_order"] == 2

        cross = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": other["id"],
                "component_kind_id": str(WEB_COMPONENT_KIND_ID),
                "name": "cross",
                "slug": "cross",
                "check_url": "https://example.com/x",
                "check_type": "http_status",
                "group_id": group["id"],
            },
        )
        assert cross.status_code == 422

        public = client.get(f"/api/status/projects/{project['slug']}")
        assert public.status_code == 200, public.text
        status_data = _data(public)
        assert status_data["groups"]
        assert status_data["groups"][0]["name"] == "Server 1"
        assert status_data["groups"][0]["services"][0]["slug"] == "api-1"

        system = client.get(f"/api/status/projects/{project['slug']}/system-status")
        assert system.status_code == 200, system.text
        system_data = _data(system)
        assert system_data["groups"][0]["name"] == "Server 1"
        assert system_data["groups"][0]["id"] == group["id"]

        deleted = client.delete(f"/api/admin/component-groups/{group['id']}")
        assert deleted.status_code in (200, 204)
        refreshed = client.get(f"/api/admin/monitored-components/{component['id']}")
        assert refreshed.status_code == 200
        assert _data(refreshed)["group_id"] is None

    def test_ungrouped_services_last(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="grp-ungrouped")
        group = _data(
            client.post(
                "/api/admin/component-groups",
                json={
                    "project_id": project["id"],
                    "name": "Server 2",
                    "slug": "server-2",
                    "sort_order": 0,
                },
            )
        )
        _create_http_component(client, project_id=project["id"], slug="lonely")
        _create_http_component(
            client,
            project_id=project["id"],
            slug="grouped",
            group_id=group["id"],
        )
        public = _data(client.get(f"/api/status/projects/{project['slug']}"))
        names = [g["name"] for g in public["groups"]]
        assert names[0] == "Server 2"
        assert names[-1] == "Ungrouped"

    def test_group_update_and_duplicate_slug(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="grp-update")
        first = _data(
            client.post(
                "/api/admin/component-groups",
                json={"project_id": project["id"], "name": "A", "slug": "server-a", "sort_order": 0},
            )
        )
        second = _data(
            client.post(
                "/api/admin/component-groups",
                json={"project_id": project["id"], "name": "B", "slug": "server-b", "sort_order": 1},
            )
        )
        updated = client.patch(
            f"/api/admin/component-groups/{first['id']}",
            json={"name": "Helsinki 1", "sort_order": 5},
        )
        assert updated.status_code == 200, updated.text
        body = _data(updated)
        assert body["name"] == "Helsinki 1"
        assert body["sort_order"] == 5

        conflict = client.patch(
            f"/api/admin/component-groups/{second['id']}",
            json={"slug": "server-a"},
        )
        assert conflict.status_code == 409

    def test_inactive_group_appears_as_ungrouped_on_public(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="grp-inactive")
        group = _data(
            client.post(
                "/api/admin/component-groups",
                json={
                    "project_id": project["id"],
                    "name": "Hidden server",
                    "slug": "hidden",
                    "is_active": False,
                },
            )
        )
        _create_http_component(
            client,
            project_id=project["id"],
            slug="hidden-api",
            group_id=group["id"],
        )
        public = _data(client.get(f"/api/status/projects/{project['slug']}"))
        assert [g["name"] for g in public["groups"]] == ["Ungrouped"]
        assert public["groups"][0]["services"][0]["group_id"] is None
