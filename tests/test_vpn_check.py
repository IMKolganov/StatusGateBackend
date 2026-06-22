import json
from unittest.mock import MagicMock, mock_open, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.models.enums import CheckOutcome, CheckType
from app.models.monitored_component import MonitoredComponent
from app.schemas.monitored_component import DEFAULT_SPEED_TEST_BYTES, MAX_SPEED_TEST_BYTES, MonitoredComponentCreate
from app.schemas.network import NetworkSummary
from app.services import vpn_check_service as vpn
from app.services.health_check_service import run_health_check
from app.services.vpn_check_service import public_network_summary, run_vpn_health_check


def _vpn_component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "Norway VPN",
        "slug": "norway-vpn",
        "check_url": "https://ifconfig.me/ip",
        "check_method": "GET",
        "check_type": CheckType.OPENVPN.value,
        "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
        "expected_status_code": 200,
        "timeout_seconds": 30,
        "is_active": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


class TestVpnHelpers:
    def test_xray_proxy_url_socks(self) -> None:
        config = {"inbounds": [{"protocol": "socks", "port": 1080, "listen": "127.0.0.1"}]}
        assert vpn._xray_proxy_url(config) == "socks5://127.0.0.1:1080"

    def test_xray_proxy_url_http(self) -> None:
        config = {"inbounds": [{"protocol": "http", "port": 8080, "listen": "0.0.0.0"}]}
        assert vpn._xray_proxy_url(config) == "http://127.0.0.1:8080"

    def test_xray_proxy_url_missing(self) -> None:
        assert vpn._xray_proxy_url({"inbounds": [{"protocol": "dokodemo-door", "port": 53}]}) is None
        assert vpn._xray_proxy_url({"inbounds": []}) is None

    def test_xray_inbound_protocol(self) -> None:
        config = {"inbounds": [{"protocol": "socks", "port": 1080}]}
        assert vpn._xray_inbound_protocol(config) == "socks"

    def test_mask_proxy(self) -> None:
        masked = vpn._mask_proxy("socks5://user:secret@127.0.0.1:1080")
        assert masked == "socks5://***:***@127.0.0.1:1080"

    def test_vpn_log_hint(self) -> None:
        log = "2025-01-01 TLS Error: TLS handshake failed\n2025-01-01 Exiting due to fatal error\n"
        assert vpn._vpn_log_hint(log) == "2025-01-01 Exiting due to fatal error"

    def test_vpn_log_hint_auth_failed(self) -> None:
        log = "AUTH: Received control message: AUTH_FAILED\n"
        assert vpn._vpn_log_hint(log) == "Authentication failed (AUTH_FAILED)"

    def test_public_network_summary(self) -> None:
        details = {
            "network": {
                "interface": "tun0",
                "ipv4_address": "10.8.0.2",
                "gateway": "10.8.0.1",
                "dns_servers": ["1.1.1.1"],
                "mtu": 1500,
                "connect_time_ms": 1200,
                "probe": {
                    "url": "https://ifconfig.me/ip",
                    "exit_ip": "203.0.113.1",
                    "latency_ms": 85,
                },
                "gateway_ping": {
                    "host": "10.8.0.1",
                    "avg_ms": 12.3,
                    "loss_percent": 0.0,
                    "jitter_ms": 1.5,
                },
                "speed_test": {
                    "ok": True,
                    "url": vpn._speed_test_url(DEFAULT_SPEED_TEST_BYTES),
                    "bytes": 524288,
                    "duration_ms": 800,
                    "mbps": 5.24,
                },
            }
        }
        summary = public_network_summary(details)
        assert summary == NetworkSummary(
            interface="tun0",
            ipv4_address="10.8.0.2",
            gateway="10.8.0.1",
            dns_servers=["1.1.1.1"],
            mtu=1500,
            connect_time_ms=1200,
            probe_url="https://ifconfig.me/ip",
            exit_ip="203.0.113.1",
            probe_latency_ms=85,
            gateway_ping_avg_ms=12.3,
            gateway_ping_loss_percent=0.0,
            gateway_ping_jitter_ms=1.5,
            download_mbps=5.24,
            download_bytes=524288,
            download_duration_ms=800,
        )

    def test_parse_ping_output(self) -> None:
        sample = """
PING 10.8.0.1 (10.8.0.1) 56(84) bytes of data.
64 bytes from 10.8.0.1: icmp_seq=1 ttl=64 time=10.2 ms

--- 10.8.0.1 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3005ms
rtt min/avg/max/mdev = 9.800/10.500/11.200/0.450 ms
"""
        parsed = vpn._parse_ping_output(sample)
        assert parsed == {
            "loss_percent": 0.0,
            "min_ms": 9.8,
            "avg_ms": 10.5,
            "max_ms": 11.2,
            "jitter_ms": 0.45,
        }

    def test_measure_download_speed(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self):
                yield b"x" * 1024

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        class FakeClient:
            def stream(self, method: str, url: str):
                return FakeStream()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("app.services.vpn_check_service.httpx.Client", return_value=FakeClient()):
            with patch("app.services.vpn_check_service.time.perf_counter", side_effect=[0.0, 1.0]):
                result = vpn._measure_download_speed("https://example.test/down", proxy_url=None, timeout=10)
        assert result is not None
        assert result["ok"] is True
        assert result["bytes"] == 1024
        assert result["duration_ms"] == 1000
        assert result["mbps"] == 0.01

    def test_public_network_summary_empty(self) -> None:
        assert public_network_summary(None) is None
        assert public_network_summary({}) is None
        assert public_network_summary({"network": "bad"}) is None

    def test_read_dns_servers(self) -> None:
        with patch("app.services.vpn_check_service.Path") as path_cls:
            path_cls.return_value.exists.return_value = True
            path_cls.return_value.read_text.return_value = "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"
            assert vpn._read_dns_servers() == ["1.1.1.1", "8.8.8.8"]

    def test_list_tun_interfaces(self) -> None:
        payload = json.dumps([{"ifname": "eth0"}, {"ifname": "tun0"}, {"ifname": "tun1"}])
        with patch("app.services.vpn_check_service.subprocess.check_output", return_value=payload):
            assert vpn._list_tun_interfaces() == ["tun0", "tun1"]

    def test_interface_is_up(self) -> None:
        payload = json.dumps(
            [
                {
                    "flags": ["UP"],
                    "addr_info": [{"family": "inet", "local": "10.8.0.2"}],
                }
            ]
        )
        with patch("app.services.vpn_check_service.subprocess.check_output", return_value=payload):
            assert vpn._interface_is_up("tun0") is True

    def test_interface_is_up_without_address(self) -> None:
        payload = json.dumps([{"flags": ["UP"], "addr_info": []}])
        with patch("app.services.vpn_check_service.subprocess.check_output", return_value=payload):
            assert vpn._interface_is_up("tun0") is False

    def test_probe_endpoint_success(self) -> None:
        request = httpx.Request("GET", "https://ifconfig.me/ip")
        response = httpx.Response(200, text="203.0.113.1\n", request=request)
        with patch("app.services.vpn_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            probe = vpn._probe_endpoint("https://ifconfig.me/ip", timeout=5)
        assert probe["ok"] is True
        assert probe["exit_ip"] == "203.0.113.1"
        assert probe["status_code"] == 200

    def test_probe_endpoint_failure(self) -> None:
        with patch("app.services.vpn_check_service.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("refused")
            probe = vpn._probe_endpoint("https://ifconfig.me/ip", timeout=5)
        assert probe["ok"] is False
        assert "refused" in probe["error"]


class TestVpnSchemas:
    def test_openvpn_create_requires_config(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "VPN",
                    "slug": "vpn",
                    "check_type": "openvpn",
                }
            )

    def test_openvpn_create_applies_defaults(self) -> None:
        payload = MonitoredComponentCreate.model_validate(
            {
                "project_id": uuid4(),
                "component_kind_id": uuid4(),
                "name": "VPN",
                "slug": "vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                "timeout_seconds": 10,
            }
        )
        assert payload.check_url == "https://ifconfig.me/ip"
        assert payload.timeout_seconds == 30

    def test_http_create_rejects_check_config(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "API",
                    "slug": "api",
                    "check_url": "https://example.com",
                    "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                }
            )

    def test_xray_create_accepts_json_config(self) -> None:
        payload = MonitoredComponentCreate.model_validate(
            {
                "project_id": uuid4(),
                "component_kind_id": uuid4(),
                "name": "Xray",
                "slug": "xray-node",
                "check_type": "xray",
                "check_config": {
                    "config_text": json.dumps({"inbounds": [{"protocol": "socks", "port": 1080}]}),
                },
            }
        )
        assert payload.check_type == "xray"

    def test_vpn_create_accepts_speed_test_bytes(self) -> None:
        payload = MonitoredComponentCreate.model_validate(
            {
                "project_id": uuid4(),
                "component_kind_id": uuid4(),
                "name": "VPN",
                "slug": "vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                "speed_test_bytes": 10_485_760,
            }
        )
        assert payload.speed_test_bytes == 10_485_760

    def test_http_create_rejects_speed_test_bytes(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "API",
                    "slug": "api",
                    "check_url": "https://example.com",
                    "speed_test_bytes": 10_485_760,
                }
            )

    def test_speed_test_bytes_for_component(self) -> None:
        component = _vpn_component(speed_test_bytes=10_485_760)
        assert vpn._speed_test_bytes_for(component) == 10_485_760
        assert vpn._speed_test_bytes_for(_vpn_component(speed_test_bytes=None)) == DEFAULT_SPEED_TEST_BYTES

    def test_vpn_create_rejects_speed_test_bytes_below_min(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "VPN",
                    "slug": "vpn",
                    "check_type": "openvpn",
                    "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                    "speed_test_bytes": 512,
                }
            )

    def test_vpn_create_rejects_speed_test_bytes_above_max(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "VPN",
                    "slug": "vpn",
                    "check_type": "openvpn",
                    "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                    "speed_test_bytes": MAX_SPEED_TEST_BYTES + 1,
                }
            )

    def test_vpn_create_rejects_non_integer_speed_test_bytes(self) -> None:
        with pytest.raises(ValidationError):
            MonitoredComponentCreate.model_validate(
                {
                    "project_id": uuid4(),
                    "component_kind_id": uuid4(),
                    "name": "VPN",
                    "slug": "vpn",
                    "check_type": "openvpn",
                    "check_config": {"config_text": "client\ndev tun\nremote x 1194\n"},
                    "speed_test_bytes": 10.5,
                }
            )


class TestOpenVpnCheck:
    def test_openvpn_tunnel_timeout(self) -> None:
        component = _vpn_component()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_tun_interface", return_value=None),
            patch("app.services.vpn_check_service._terminate_process") as terminate,
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.TIMEOUT.value
        assert result.error_message == "OpenVPN tunnel did not come up in time"
        terminate.assert_called_once()

    def test_openvpn_success(self) -> None:
        component = _vpn_component()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        network = {
            "ipv4_address": "10.8.0.2",
            "gateway": "10.8.0.1",
            "dns_servers": ["1.1.1.1"],
            "routes": [],
            "ipv4_addresses": ["10.8.0.2"],
            "ipv6_addresses": [],
        }

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_tun_interface", return_value="tun0"),
            patch("app.services.vpn_check_service._collect_network_details", return_value=network),
            patch(
                "app.services.vpn_check_service._probe_endpoint",
                return_value={"ok": True, "status_code": 200, "exit_ip": "203.0.113.1", "latency_ms": 42},
            ),
            patch("app.services.vpn_check_service._enrich_network_metrics"),
            patch("app.services.vpn_check_service._terminate_process"),
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.UP.value
        assert result.details["network"]["interface"] == "tun0"
        assert result.details["network"]["ipv4_address"] == "10.8.0.2"
        assert result.details["network"]["probe"]["exit_ip"] == "203.0.113.1"

    def test_openvpn_probe_failure(self) -> None:
        component = _vpn_component()
        mock_proc = MagicMock()

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_tun_interface", return_value="tun0"),
            patch("app.services.vpn_check_service._collect_network_details", return_value={"routes": []}),
            patch(
                "app.services.vpn_check_service._probe_endpoint",
                return_value={"ok": False, "error": "probe failed"},
            ),
            patch("app.services.vpn_check_service._terminate_process"),
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.DOWN.value
        assert result.error_message == "probe failed"


class TestXrayCheck:
    def test_xray_invalid_json(self) -> None:
        component = _vpn_component(
            check_type=CheckType.XRAY.value,
            check_config={"config_text": "{not-json"},
        )
        result = run_vpn_health_check(component)
        assert result.outcome == CheckOutcome.ERROR.value
        assert "Invalid Xray config" in (result.error_message or "")

    def test_xray_missing_inbound(self) -> None:
        component = _vpn_component(
            check_type=CheckType.XRAY.value,
            check_config={"config_text": json.dumps({"inbounds": []})},
        )
        result = run_vpn_health_check(component)
        assert result.outcome == CheckOutcome.ERROR.value
        assert "socks or http inbound" in (result.error_message or "")

    def test_xray_proxy_timeout(self) -> None:
        component = _vpn_component(
            check_type=CheckType.XRAY.value,
            check_config={"config_text": json.dumps({"inbounds": [{"protocol": "socks", "port": 1080}]})},
        )
        mock_proc = MagicMock()

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_proxy", return_value=False),
            patch("app.services.vpn_check_service._terminate_process"),
            patch("builtins.open", mock_open()),
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.TIMEOUT.value
        assert result.error_message == "Xray proxy did not become ready in time"

    def test_xray_success(self) -> None:
        component = _vpn_component(
            check_type=CheckType.XRAY.value,
            check_config={"config_text": json.dumps({"inbounds": [{"protocol": "socks", "port": 1080}]})},
        )
        mock_proc = MagicMock()

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_proxy", return_value=True),
            patch(
                "app.services.vpn_check_service._probe_endpoint",
                return_value={"ok": True, "status_code": 200, "exit_ip": "198.51.100.9", "latency_ms": 55},
            ),
            patch("app.services.vpn_check_service._enrich_network_metrics"),
            patch("app.services.vpn_check_service._terminate_process"),
            patch("builtins.open", mock_open()),
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.UP.value
        assert result.details["network"]["proxy_url"] == "socks5://127.0.0.1:1080"
        assert result.details["network"]["inbound_protocol"] == "socks"

    def test_xray_success_from_vless_share_link(self) -> None:
        component = _vpn_component(
            check_type=CheckType.XRAY.value,
            check_config={
                "config_text": (
                    "vless://00000000-0000-4000-8000-000000000001@example.com:443"
                    "?encryption=none&security=tls&sni=example.com&type=tcp"
                ),
            },
        )
        mock_proc = MagicMock()

        with (
            patch("app.services.vpn_check_service.subprocess.Popen", return_value=mock_proc),
            patch("app.services.vpn_check_service._wait_for_proxy", return_value=True),
            patch(
                "app.services.vpn_check_service._probe_endpoint",
                return_value={"ok": True, "status_code": 200, "exit_ip": "198.51.100.9", "latency_ms": 55},
            ),
            patch("app.services.vpn_check_service._enrich_network_metrics"),
            patch("app.services.vpn_check_service._terminate_process"),
            patch("builtins.open", mock_open()),
        ):
            result = run_vpn_health_check(component)

        assert result.outcome == CheckOutcome.UP.value


class TestHealthCheckDispatch:
    def test_run_health_check_routes_openvpn(self) -> None:
        component = _vpn_component()
        with patch("app.services.health_check_service.run_vpn_health_check") as vpn_check:
            vpn_check.return_value = MagicMock(outcome=CheckOutcome.UP.value)
            run_health_check(component)
        vpn_check.assert_called_once_with(component)

    def test_run_health_check_routes_http(self) -> None:
        component = _vpn_component(check_type=CheckType.HTTP_STATUS.value, check_config=None)
        with (
            patch("app.services.health_check_service.run_vpn_health_check") as vpn_check,
            patch("app.services.health_check_service.httpx.Client") as client_cls,
        ):
            request = httpx.Request("GET", component.check_url)
            client_cls.return_value.__enter__.return_value.request.return_value = httpx.Response(200, request=request)
            run_health_check(component)
        vpn_check.assert_not_called()
