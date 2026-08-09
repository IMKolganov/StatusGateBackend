import pyotp
from fastapi.testclient import TestClient


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


def _register_and_login(client: TestClient, email: str = "mfa@example.com", password: str = "password123") -> None:
    client.post("/api/auth/register", json={"email": email, "password": password})
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    assert "sg_access_token" in login.cookies


def _setup_and_enable_totp(client: TestClient, password: str = "password123") -> str:
    setup = client.post("/api/auth/2fa/setup")
    assert setup.status_code == 200, setup.text
    secret = _data(setup)["secret"]
    code = pyotp.TOTP(secret).now()
    enabled = client.post("/api/auth/2fa/enable", json={"code": code})
    assert enabled.status_code == 200, enabled.text
    assert _data(enabled)["is_totp_enabled"] is True
    return secret


class TestAuthMfa:
    def test_setup_2fa_returns_secret(self, client: TestClient) -> None:
        _register_and_login(client)
        response = client.post("/api/auth/2fa/setup")
        assert response.status_code == 200
        payload = _data(response)
        assert payload["secret"]
        assert payload["otpauth_url"].startswith("otpauth://")
        assert payload["qr_code_base64"]

    def test_enable_2fa_with_valid_code(self, client: TestClient) -> None:
        _register_and_login(client)
        setup = client.post("/api/auth/2fa/setup")
        secret = _data(setup)["secret"]
        response = client.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()})
        assert response.status_code == 200
        assert _data(response)["is_totp_enabled"] is True

    def test_enable_2fa_with_bad_code(self, client: TestClient) -> None:
        _register_and_login(client)
        client.post("/api/auth/2fa/setup")
        response = client.post("/api/auth/2fa/enable", json={"code": "000000"})
        assert response.status_code == 400
        assert "invalid 2fa" in _error_message(response).lower()

    def test_login_returns_mfa_token_when_totp_enabled(self, client: TestClient) -> None:
        _register_and_login(client, email="mfa-login@example.com")
        _setup_and_enable_totp(client)
        client.cookies.clear()

        login = client.post(
            "/api/auth/login",
            json={"email": "mfa-login@example.com", "password": "password123"},
        )
        assert login.status_code == 200
        assert "sg_access_token" not in login.cookies
        mfa = _data(login)
        assert mfa["mfa_required"] is True
        assert mfa["mfa_token"]

    def test_login_2fa_success(self, client: TestClient) -> None:
        email = "mfa-ok@example.com"
        password = "password123"
        _register_and_login(client, email=email, password=password)
        secret = _setup_and_enable_totp(client, password=password)
        client.cookies.clear()

        login = client.post("/api/auth/login", json={"email": email, "password": password})
        mfa_token = _data(login)["mfa_token"]
        verify = client.post(
            "/api/auth/login/2fa",
            json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
        )
        assert verify.status_code == 200
        assert "sg_access_token" in verify.cookies
        assert "sg_refresh_token" in verify.cookies
        assert _data(verify)["email"] == email

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert _data(me)["email"] == email

    def test_login_2fa_bad_code(self, client: TestClient) -> None:
        email = "mfa-bad@example.com"
        _register_and_login(client, email=email)
        _setup_and_enable_totp(client)
        client.cookies.clear()

        login = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        mfa_token = _data(login)["mfa_token"]
        verify = client.post(
            "/api/auth/login/2fa",
            json={"mfa_token": mfa_token, "code": "000000"},
        )
        assert verify.status_code == 401
        assert "invalid 2fa" in _error_message(verify).lower()

    def test_disable_2fa(self, client: TestClient) -> None:
        password = "password123"
        _register_and_login(client, email="mfa-off@example.com", password=password)
        secret = _setup_and_enable_totp(client, password=password)

        disabled = client.post(
            "/api/auth/2fa/disable",
            json={"password": password, "code": pyotp.TOTP(secret).now()},
        )
        assert disabled.status_code == 200
        assert _data(disabled)["is_totp_enabled"] is False

        client.cookies.clear()
        login = client.post(
            "/api/auth/login",
            json={"email": "mfa-off@example.com", "password": password},
        )
        assert login.status_code == 200
        assert "sg_access_token" in login.cookies
        account = _data(login)
        assert account["is_totp_enabled"] is False
        assert "mfa_token" not in account

    def test_setup_2fa_without_auth_returns_401(self, client: TestClient) -> None:
        response = client.post("/api/auth/2fa/setup")
        assert response.status_code == 401
        assert "not authenticated" in _error_message(response).lower()
