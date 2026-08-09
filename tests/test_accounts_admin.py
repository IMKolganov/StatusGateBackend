from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

TEST_ADMIN_EMAIL = "admin@example.com"
TEST_PASSWORD = "password123"


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


def _login_as(client: TestClient, email: str, password: str = TEST_PASSWORD) -> None:
    client.cookies.clear()
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


class TestAccountsAdmin:
    def test_admin_list_accounts(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get("/api/admin/accounts", headers=admin_headers)
        assert response.status_code == 200
        page = _data(response)
        assert page["total"] >= 1
        emails = {item["email"] for item in page["items"]}
        assert TEST_ADMIN_EMAIL in emails

    def test_admin_get_account_by_id(self, client: TestClient, admin_headers: dict) -> None:
        listing = _data(client.get("/api/admin/accounts", headers=admin_headers))
        account_id = listing["items"][0]["id"]
        response = client.get(f"/api/admin/accounts/{account_id}", headers=admin_headers)
        assert response.status_code == 200
        account = _data(response)
        assert account["id"] == account_id
        assert account["email"] == TEST_ADMIN_EMAIL
        assert account["is_active"] is True

    def test_admin_update_roles(self, client: TestClient, admin_headers: dict) -> None:
        created = _data(
            client.post(
                "/api/auth/register",
                json={"email": "role-target@example.com", "password": TEST_PASSWORD},
            )
        )
        account_id = created["id"]
        response = client.put(
            f"/api/admin/accounts/{account_id}/roles",
            json={"access_roles": ["operator", "viewer"]},
            headers=admin_headers,
        )
        assert response.status_code == 200
        updated = _data(response)
        assert set(updated["access_roles"]) == {"operator", "viewer"}

    def test_admin_deactivate_and_activate(self, client: TestClient, admin_headers: dict) -> None:
        created = _data(
            client.post(
                "/api/auth/register",
                json={"email": "toggle@example.com", "password": TEST_PASSWORD},
            )
        )
        account_id = created["id"]

        deactivated = client.post(
            f"/api/admin/accounts/{account_id}/deactivate",
            headers=admin_headers,
        )
        assert deactivated.status_code == 200
        assert _data(deactivated)["is_active"] is False

        activated = client.post(
            f"/api/admin/accounts/{account_id}/activate",
            headers=admin_headers,
        )
        assert activated.status_code == 200
        assert _data(activated)["is_active"] is True

    def test_inactive_account_cannot_login(self, client: TestClient, admin_headers: dict) -> None:
        created = _data(
            client.post(
                "/api/auth/register",
                json={"email": "inactive@example.com", "password": TEST_PASSWORD},
            )
        )
        deactivate = client.post(
            f"/api/admin/accounts/{created['id']}/deactivate",
            headers=admin_headers,
        )
        assert deactivate.status_code == 200

        client.cookies.clear()
        login = client.post(
            "/api/auth/login",
            json={"email": "inactive@example.com", "password": TEST_PASSWORD},
        )
        assert login.status_code == 403
        assert "inactive" in _error_message(login).lower()

    def test_registration_disabled_blocks_new_accounts(
        self,
        client: TestClient,
        admin_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.config.settings.allow_registration", False)
        monkeypatch.setattr("app.services.auth_service.settings.allow_registration", False)

        status = client.get("/api/auth/registration-status")
        assert status.status_code == 200
        assert _data(status)["allow_registration"] is False

        response = client.post(
            "/api/auth/register",
            json={"email": "blocked@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 403
        assert "registration is disabled" in _error_message(response).lower()

    def test_non_admin_forbidden_on_admin_accounts(self, client: TestClient, admin_headers: dict) -> None:
        second = _data(
            client.post(
                "/api/auth/register",
                json={"email": "user@example.com", "password": TEST_PASSWORD},
            )
        )
        assert second["access_roles"] == ["user"]

        _login_as(client, "user@example.com")
        response = client.get("/api/admin/accounts")
        assert response.status_code == 403
        assert "insufficient permissions" in _error_message(response).lower()

        by_id = client.get(f"/api/admin/accounts/{second['id']}")
        assert by_id.status_code == 403

    def test_unknown_account_id_returns_404(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get(f"/api/admin/accounts/{uuid4()}", headers=admin_headers)
        assert response.status_code == 404
        assert "not found" in _error_message(response).lower()
