import pytest
from fastapi.testclient import TestClient


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


class TestAuthRegistration:
    def test_first_account_gets_admin_role(self, client: TestClient) -> None:
        response = client.post(
            "/api/auth/register",
            json={"email": "first@example.com", "password": "password123"},
        )
        assert response.status_code == 200
        account = _data(response)
        assert account["email"] == "first@example.com"
        assert "admin" in account["access_roles"]

    def test_second_account_gets_user_role(self, client: TestClient) -> None:
        client.post(
            "/api/auth/register",
            json={"email": "first@example.com", "password": "password123"},
        )
        response = client.post(
            "/api/auth/register",
            json={"email": "second@example.com", "password": "password123"},
        )
        assert response.status_code == 200
        account = _data(response)
        assert account["access_roles"] == ["user"]

    def test_duplicate_email_rejected(self, client: TestClient) -> None:
        payload = {"email": "dup@example.com", "password": "password123"}
        first = client.post("/api/auth/register", json=payload)
        assert first.status_code == 200

        duplicate = client.post("/api/auth/register", json=payload)
        assert duplicate.status_code == 409
        assert "already registered" in _error_message(duplicate).lower()

    def test_registration_status_reflects_settings(self, client: TestClient) -> None:
        response = client.get("/api/auth/registration-status")
        assert response.status_code == 200
        status = _data(response)
        assert status["allow_registration"] is True
        assert status["require_email_verification"] is False


class TestAuthLogin:
    def test_login_sets_session_cookie(self, client: TestClient) -> None:
        client.post(
            "/api/auth/register",
            json={"email": "login@example.com", "password": "password123"},
        )
        login = client.post(
            "/api/auth/login",
            json={"email": "login@example.com", "password": "password123"},
        )
        assert login.status_code == 200
        assert "sg_access_token" in login.cookies

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert _data(me)["email"] == "login@example.com"

    def test_invalid_credentials_rejected(self, client: TestClient) -> None:
        client.post(
            "/api/auth/register",
            json={"email": "login@example.com", "password": "password123"},
        )
        response = client.post(
            "/api/auth/login",
            json={"email": "login@example.com", "password": "wrong-password"},
        )
        assert response.status_code == 401
