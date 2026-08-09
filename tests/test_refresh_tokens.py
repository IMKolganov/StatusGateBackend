from fastapi.testclient import TestClient

from app.config import settings


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


def _login(client: TestClient, email: str = "refresh@example.com", password: str = "password123") -> None:
    client.post("/api/auth/register", json={"email": email, "password": password})
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    assert settings.access_cookie_name in login.cookies
    assert settings.refresh_cookie_name in login.cookies


class TestRefreshTokens:
    def test_login_then_refresh_rotates_cookies(self, client: TestClient) -> None:
        _login(client)
        old_access = client.cookies.get(settings.access_cookie_name)
        old_refresh = client.cookies.get(settings.refresh_cookie_name)
        assert old_access
        assert old_refresh

        refreshed = client.post("/api/auth/refresh")
        assert refreshed.status_code == 200
        assert _data(refreshed)["email"] == "refresh@example.com"

        new_access = client.cookies.get(settings.access_cookie_name)
        new_refresh = client.cookies.get(settings.refresh_cookie_name)
        assert new_access
        assert new_refresh
        assert new_access != old_access
        assert new_refresh != old_refresh

    def test_refresh_without_cookie_returns_401(self, client: TestClient) -> None:
        response = client.post("/api/auth/refresh")
        assert response.status_code == 401
        assert "refresh token missing" in _error_message(response).lower()

    def test_logout_clears_cookies_and_refresh_fails(self, client: TestClient) -> None:
        _login(client, email="logout@example.com")
        old_refresh = client.cookies.get(settings.refresh_cookie_name)
        assert old_refresh

        logout = client.post("/api/auth/logout")
        # GlobalExceptionMiddleware normalizes 204 → success envelope with status 200.
        assert logout.status_code == 200

        # Server revokes the refresh token. Even if the jar still holds a cookie
        # (204→200 rewrite may drop Set-Cookie delete headers), reuse must fail.
        client.cookies.set(settings.refresh_cookie_name, old_refresh, path="/api/auth")
        refresh = client.post("/api/auth/refresh")
        assert refresh.status_code == 401

    def test_revoked_refresh_reuse_returns_401(self, client: TestClient) -> None:
        _login(client, email="reuse@example.com")
        old_refresh = client.cookies.get(settings.refresh_cookie_name)
        assert old_refresh

        rotated = client.post("/api/auth/refresh")
        assert rotated.status_code == 200
        assert client.cookies.get(settings.refresh_cookie_name) != old_refresh

        client.cookies.set(settings.refresh_cookie_name, old_refresh, path="/api/auth")
        reuse = client.post("/api/auth/refresh")
        assert reuse.status_code == 401
        assert "invalid refresh" in _error_message(reuse).lower()
