import json

import pytest

from app.services.xray_config import parse_xray_config_text, vless_uri_to_config


SAMPLE_VLESS = (
    "vless://16e5f3d7-0000-4000-8000-000000d4c3d@xs2.datagateapp.com:8443"
    "?encryption=none&security=tls&sni=xs2.datagateapp.com&type=tcp#DataGate+Norway+xray"
)


class TestXrayConfigParsing:
    def test_vless_share_link_builds_socks_inbound(self) -> None:
        config = vless_uri_to_config(SAMPLE_VLESS)

        assert config["inbounds"][0]["protocol"] == "socks"
        assert config["inbounds"][0]["port"] == 1080
        assert config["outbounds"][0]["protocol"] == "vless"
        assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "xs2.datagateapp.com"
        assert config["outbounds"][0]["settings"]["vnext"][0]["port"] == 8443
        assert config["outbounds"][0]["streamSettings"]["security"] == "tls"
        assert config["outbounds"][0]["streamSettings"]["tlsSettings"]["serverName"] == "xs2.datagateapp.com"

    def test_vless_with_comment_lines(self) -> None:
        config_text = "\n".join(
            [
                SAMPLE_VLESS,
                "# Norway xray",
                "UUID: 16e5f3d7-0000-4000-8000-000000d4c3d",
                "Endpoint: xs2.datagateapp.com:8443",
            ]
        )
        config = parse_xray_config_text(config_text)
        assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "xs2.datagateapp.com"

    def test_json_config_still_supported(self) -> None:
        raw = json.dumps({"inbounds": [{"protocol": "socks", "port": 1080}]})
        assert parse_xray_config_text(raw)["inbounds"][0]["port"] == 1080

    def test_pretty_printed_multiline_json(self) -> None:
        raw = """
{
  "inbounds": [
    {"protocol": "socks", "port": 1080}
  ],
  "outbounds": [
    {"protocol": "freedom"}
  ]
}
"""
        config = parse_xray_config_text(raw)
        assert config["inbounds"][0]["port"] == 1080
        assert config["outbounds"][0]["protocol"] == "freedom"

    def test_datagate_android_json_profile_uses_vless_field(self) -> None:
        raw = json.dumps(
            {
                "dnsServers": ["8.8.8.8"],
                "dnsIdentityEnabled": False,
                "vless": SAMPLE_VLESS,
                "uuid": "16e5f3d7-0000-4000-8000-000000d4c3d",
                "endpoint": "xs2.datagateapp.com:8443",
                "friendlyName": "Norway xray",
            }
        )
        config = parse_xray_config_text(raw)
        assert config["inbounds"][0]["protocol"] == "socks"
        assert config["outbounds"][0]["protocol"] == "vless"
        assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "xs2.datagateapp.com"

    def test_outbound_only_json_gets_socks_inbound(self) -> None:
        raw = json.dumps(
            {
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"vnext": [{"address": "x.example.com", "port": 443, "users": []}]},
                    }
                ]
            }
        )
        config = parse_xray_config_text(raw)
        assert config["inbounds"][0]["protocol"] == "socks"
        assert config["inbounds"][0]["port"] == 1080
        assert config["routing"]["rules"][0]["outboundTag"] == "proxy"

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="JSON or a vless://"):
            parse_xray_config_text("not-a-config")

    def test_json_without_proxy_raises(self) -> None:
        with pytest.raises(ValueError, match="socks or http inbound"):
            parse_xray_config_text(json.dumps({"inbounds": [], "outbounds": [{"protocol": "freedom"}]}))

    def test_vless_ws_params(self) -> None:
        uri = (
            "vless://00000000-0000-4000-8000-000000000001@example.com:443"
            "?type=ws&security=tls&path=/vpn&host=cdn.example.com&sni=cdn.example.com"
        )
        config = vless_uri_to_config(uri)
        stream = config["outbounds"][0]["streamSettings"]
        assert stream["network"] == "ws"
        assert stream["wsSettings"]["path"] == "/vpn"
        assert stream["wsSettings"]["headers"]["Host"] == "cdn.example.com"
