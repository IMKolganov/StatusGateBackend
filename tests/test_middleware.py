import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.middleware.global_exception import GlobalExceptionMiddleware, register_exception_handlers
from app.middleware.trace_id import TraceIdMiddleware


def _error_message(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["message"]


class TestTraceIdMiddleware:
    def test_echoes_x_trace_id_header(self, client: TestClient) -> None:
        response = client.get("/api/auth/registration-status", headers={"X-Trace-Id": "trace-from-client"})
        assert response.status_code == 200
        assert response.headers.get("X-Trace-Id") == "trace-from-client"

    def test_generates_x_trace_id_when_missing(self, client: TestClient) -> None:
        response = client.get("/api/auth/registration-status")
        assert response.status_code == 200
        trace_id = response.headers.get("X-Trace-Id")
        assert trace_id
        assert len(trace_id) >= 8


class TestRequireHttpsMiddleware:
    def test_rejects_http_api_when_require_https(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.config.settings.require_https", True)
        monkeypatch.setattr("app.middleware.https.settings.require_https", True)

        response = client.get("/api/auth/me")
        assert response.status_code == 403
        assert "https is required" in _error_message(response).lower()

    def test_allows_health_over_http_when_require_https(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.config.settings.require_https", True)
        monkeypatch.setattr("app.middleware.https.settings.require_https", True)

        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"


def _mini_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(GlobalExceptionMiddleware)
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)

    @app.get("/boom-value")
    def boom_value():
        raise ValueError("bad input")

    @app.get("/boom-integrity")
    def boom_integrity():
        raise IntegrityError("stmt", {}, Exception("duplicate key"))

    @app.get("/boom-http")
    def boom_http():
        raise HTTPException(status_code=418, detail="teapot")

    @app.get("/plain-ok")
    def plain_ok():
        return {"hello": "world"}

    return app


class TestGlobalExceptionMiddlewareLeftovers:
    def test_maps_value_error_to_400(self) -> None:
        client = TestClient(_mini_app(), raise_server_exceptions=False)
        response = client.get("/boom-value")
        assert response.status_code == 400
        assert response.json()["message"] == "bad input"
        assert response.headers.get("X-Trace-Id")

    def test_maps_integrity_error_to_409(self) -> None:
        client = TestClient(_mini_app(), raise_server_exceptions=False)
        response = client.get("/boom-integrity")
        assert response.status_code == 409
        assert "already exists" in response.json()["message"].lower()

    def test_wraps_success_payload(self) -> None:
        client = TestClient(_mini_app())
        response = client.get("/plain-ok")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {"hello": "world"}

    def test_http_exception_uses_detail_message(self) -> None:
        client = TestClient(_mini_app(), raise_server_exceptions=False)
        response = client.get("/boom-http")
        assert response.status_code == 418
        assert response.json()["message"] == "teapot"
