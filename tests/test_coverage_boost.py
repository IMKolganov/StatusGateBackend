"""Targeted coverage for OpenAPI customize, Google token verify, curl speed, VPN helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import google_token
from app.core.openapi import _customize_openapi_schema, setup_openapi
from app.main import app
from app.services import speed_measure, vpn_netns, vpn_process_utils
from app.services.xray_config import parse_xray_config_text, vless_uri_to_config


class TestOpenApiCustomize:
    def test_openapi_json_has_wrapped_login_and_error_schemas(self, client: TestClient) -> None:
        app.openapi_schema = None
        setup_openapi(app)
        schema = client.get("/openapi.json").json()
        assert "paths" in schema
        login = schema["paths"]["/api/auth/login"]["post"]["responses"]["200"]
        assert "ApiResponse_LoginResult" in json.dumps(login)
        # Cached schema path
        again = client.get("/openapi.json").json()
        assert again["info"]["title"] == schema["info"]["title"]

    def test_customize_wraps_204_and_error_status(self) -> None:
        raw = {
            "paths": {
                "/api/demo": {
                    "delete": {
                        "responses": {
                            "204": {"description": "gone"},
                            "400": {
                                "description": "bad",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            },
                            "200": {
                                "description": "ok",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AccountResponse"}}},
                            },
                        }
                    }
                },
                "/api/empty": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {"application/json": {"schema": {}}},
                            }
                        }
                    }
                },
            },
            "components": {"schemas": {}},
        }
        out = _customize_openapi_schema(raw)
        delete_responses = out["paths"]["/api/demo"]["delete"]["responses"]
        assert "204" not in delete_responses
        assert "200" in delete_responses
        assert "ApiErrorData" in out["components"]["schemas"]


class TestGoogleTokenVerify:
    def setup_method(self) -> None:
        google_token._jwk_client = None

    def test_invalid_jwt_raises_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(google_token.settings, "google_client_id", "client.apps.googleusercontent.com")

        class Boom(jwt.PyJWTError):
            pass

        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.side_effect = Boom("bad")
        monkeypatch.setattr(google_token, "_get_jwk_client", lambda: mock_client)

        with pytest.raises(HTTPException) as exc:
            google_token.verify_google_id_token("token")
        assert exc.value.status_code == 401

    def test_unverified_email_and_missing_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(google_token.settings, "google_client_id", "client.apps.googleusercontent.com")
        key = MagicMock()
        key.key = "secret"
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = key
        monkeypatch.setattr(google_token, "_get_jwk_client", lambda: mock_client)

        monkeypatch.setattr(
            google_token.jwt,
            "decode",
            lambda *a, **k: {"email_verified": False, "sub": "1", "email": "a@b.c"},
        )
        with pytest.raises(HTTPException) as exc:
            google_token.verify_google_id_token("token")
        assert exc.value.status_code == 401

        monkeypatch.setattr(
            google_token.jwt,
            "decode",
            lambda *a, **k: {"email_verified": True, "sub": None, "email": None},
        )
        with pytest.raises(HTTPException) as exc2:
            google_token.verify_google_id_token("token")
        assert exc2.value.status_code == 400

    def test_happy_path_with_picture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(google_token.settings, "google_client_id", "client.apps.googleusercontent.com")
        key = MagicMock()
        key.key = "secret"
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = key
        monkeypatch.setattr(google_token, "_get_jwk_client", lambda: mock_client)
        monkeypatch.setattr(
            google_token.jwt,
            "decode",
            lambda *a, **k: {
                "email_verified": "true",
                "sub": "sub-1",
                "email": "u@example.com",
                "name": "User",
                "picture": "https://img",
            },
        )
        profile = google_token.verify_google_id_token("token")
        assert profile["sub"] == "sub-1"
        assert profile["picture"] == "https://img"
        # Cache client
        assert google_token._get_jwk_client() is mock_client


class TestVpnProcessUtils:
    def test_log_hints_cover_markers(self) -> None:
        assert vpn_process_utils._vpn_log_hint(None) is None
        assert "AUTH_FAILED" in (vpn_process_utils._vpn_log_hint("x\nAUTH_FAILED\n") or "")
        assert "TLS ERROR" in (vpn_process_utils._vpn_log_hint("TLS ERROR: boom") or "")
        assert "CANNOT RESOLVE" in (vpn_process_utils._vpn_log_hint("CANNOT RESOLVE host") or "")
        assert "CONNECTION REFUSED" in (vpn_process_utils._vpn_log_hint("CONNECTION REFUSED") or "")
        assert "INACTIVITY TIMEOUT" in (vpn_process_utils._vpn_log_hint("INACTIVITY TIMEOUT") or "")
        assert "VERIFY" in (vpn_process_utils._vpn_log_hint("CERTIFICATE VERIFY FAILED") or "")
        assert " FATAL" in (vpn_process_utils._vpn_log_hint("something FATAL happened") or "") or "FATAL" in (
            vpn_process_utils._vpn_log_hint("something FATAL happened") or ""
        )
        assert vpn_process_utils._vpn_log_hint("\n\ninfo only\n") is None

    def test_read_tail_and_mask_proxy(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.log"
        assert vpn_process_utils._read_tail(missing) is None
        empty = tmp_path / "empty.log"
        empty.write_text("", encoding="utf-8")
        assert vpn_process_utils._read_tail(empty) is None
        filled = tmp_path / "filled.log"
        filled.write_text("abcdefghij", encoding="utf-8")
        assert vpn_process_utils._read_tail(filled, max_chars=4) == "ghij"
        assert vpn_process_utils._mask_proxy("socks5://user:pass@host:1080") == "socks5://***:***@host:1080"

    def test_terminate_process_paths(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "pid"
        pid_path.write_text("not-an-int", encoding="utf-8")
        proc = MagicMock()
        proc.poll.return_value = 0
        vpn_process_utils._terminate_process(proc, pid_path)

        pid_path.write_text("12345", encoding="utf-8")
        with patch("app.services.vpn_process_utils.os.kill", side_effect=OSError):
            vpn_process_utils._terminate_process(proc, pid_path)

        proc2 = MagicMock()
        proc2.poll.return_value = None
        proc2.pid = 99
        proc2.wait.side_effect = __import__("subprocess").TimeoutExpired(cmd="x", timeout=1)
        with patch("app.services.vpn_process_utils.os.killpg", side_effect=ProcessLookupError):
            vpn_process_utils._terminate_process(proc2, None)
            proc2.terminate.assert_called()
            proc2.kill.assert_called()


class TestVpnNetnsExtras:
    def test_list_netns_json_and_text_fallback(self) -> None:
        with patch(
            "app.services.vpn_netns.subprocess.check_output",
            return_value=json.dumps([{"name": "sg-a"}, {"name": 1}, "x"]),
        ):
            assert vpn_netns.list_netns_names() == {"sg-a"}

        with patch(
            "app.services.vpn_netns.subprocess.check_output",
            side_effect=[FileNotFoundError, "sg-b (id: 0)\n\n"],
        ):
            assert "sg-b" in vpn_netns.list_netns_names()

        with patch(
            "app.services.vpn_netns.subprocess.check_output",
            side_effect=[__import__("subprocess").CalledProcessError(1, "ip"), FileNotFoundError],
        ):
            assert vpn_netns.list_netns_names() == set()

        with patch("app.services.vpn_netns.subprocess.check_output", return_value="{bad"):
            assert vpn_netns.list_netns_names() == set()

    def test_delete_netns_and_resolv(self, tmp_path: Path) -> None:
        with patch("app.services.vpn_netns.subprocess.run") as run:
            vpn_netns.delete_netns("sg-x")
            run.assert_called()

        with patch("app.services.vpn_netns.Path") as path_cls:
            path_cls.side_effect = OSError("denied")
            vpn_netns.ensure_netns_resolv("sg-y")

    def test_name_helpers(self) -> None:
        cid = uuid4()
        assert vpn_netns.netns_name_for_component(cid).startswith("sg-")
        assert vpn_netns.tun_name_for_component(cid).startswith("tun-")


class TestSpeedMeasureCurl:
    def test_download_curl_success_and_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speed_measure.time, "perf_counter", MagicMock(side_effect=[0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]))

        ok = MagicMock(returncode=0, stdout="1250000", stderr="")
        with patch("app.services.speed_measure.subprocess.run", return_value=ok):
            result = speed_measure.measure_download_speed_curl("https://x", timeout=5, netns="ns1")
            assert result and result["ok"] is True
            assert result["bytes"] == 1250000

        fail = MagicMock(returncode=1, stdout="", stderr="curl fail")
        with patch("app.services.speed_measure.subprocess.run", return_value=fail):
            result = speed_measure.measure_download_speed_curl("https://x", timeout=5, netns="ns1")
            assert result and result["ok"] is False

        empty = MagicMock(returncode=0, stdout="0", stderr="")
        with patch("app.services.speed_measure.subprocess.run", return_value=empty):
            result = speed_measure.measure_download_speed_curl("https://x", timeout=5, netns="ns1")
            assert result and result["ok"] is False

        with patch("app.services.speed_measure.subprocess.run", side_effect=ValueError("bad")):
            result = speed_measure.measure_download_speed_curl("https://x", timeout=5, netns="ns1")
            assert result and result["ok"] is False

    def test_upload_curl_success_and_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speed_measure.time, "perf_counter", MagicMock(side_effect=[0.0, 1.0] * 6))

        ok = MagicMock(returncode=0, stdout="500000", stderr="")
        with patch("app.services.speed_measure.subprocess.run", return_value=ok):
            result = speed_measure.measure_upload_speed_curl("https://x", bytes_count=500000, timeout=5, netns="ns1")
            assert result and result["ok"] is True

        fail = MagicMock(returncode=22, stdout="", stderr="upload fail")
        with patch("app.services.speed_measure.subprocess.run", return_value=fail):
            result = speed_measure.measure_upload_speed_curl("https://x", bytes_count=100, timeout=5, netns="ns1")
            assert result and result["ok"] is False

        zero = MagicMock(returncode=0, stdout="0", stderr="")
        with patch("app.services.speed_measure.subprocess.run", return_value=zero):
            result = speed_measure.measure_upload_speed_curl("https://x", bytes_count=0, timeout=5, netns="ns1")
            assert result and result["ok"] is False

        with patch("app.services.speed_measure.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="x", timeout=1)):
            result = speed_measure.measure_upload_speed_curl("https://x", bytes_count=10, timeout=5, netns="ns1")
            assert result and result["ok"] is False


class TestXrayConfigEdges:
    def test_grpc_and_reality_params(self) -> None:
        uri = (
            "vless://00000000-0000-4000-8000-000000000099@host.example:443"
            "?type=grpc&security=reality&pbk=pub&sid=abcd&sni=host.example&serviceName=tunnel"
            "&fp=chrome&spx=%2F"
        )
        config = vless_uri_to_config(uri)
        stream = config["outbounds"][0]["streamSettings"]
        assert stream["network"] == "grpc"
        assert stream["security"] == "reality"

    def test_invalid_json_object(self) -> None:
        with pytest.raises(ValueError):
            parse_xray_config_text("[1,2,3]")

    def test_pretty_printed_json_config(self) -> None:
        text = '{\n  "inbounds": [],\n  "outbounds": [{"protocol": "freedom"}]\n}\n'
        config = parse_xray_config_text(text)
        assert config["outbounds"][0]["protocol"] == "freedom"

    def test_datagate_vless_wrapper(self) -> None:
        text = (
            '{\n  "vless": "vless://00000000-0000-4000-8000-000000000099@xs2.example.com:443'
            '?encryption=none&security=tls&sni=xs2.example.com&type=tcp#Norway",\n'
            '  "dnsServers": ["1.1.1.1"],\n'
            '  "uuid": "00000000-0000-4000-8000-000000000099",\n'
            '  "endpoint": "xs2.example.com:443"\n}\n'
        )
        config = parse_xray_config_text(text)
        assert any(i.get("protocol") == "socks" for i in config["inbounds"])
        assert config["outbounds"][0]["protocol"] == "vless"
        assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "xs2.example.com"


class TestGlobalExceptionDocsSkip:
    def test_docs_and_health_skip_envelope(self, client: TestClient) -> None:
        health = client.get("/health")
        assert health.status_code == 200
        # openapi should still load
        assert client.get("/openapi.json").status_code == 200
