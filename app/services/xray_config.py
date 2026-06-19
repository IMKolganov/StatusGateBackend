import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_SOCKS_PORT = 1080
DEFAULT_SOCKS_LISTEN = "127.0.0.1"
_VLESS_URI_PATTERN = re.compile(r"^vless://", re.IGNORECASE)


def parse_xray_config_text(config_text: str) -> dict[str, Any]:
    """Accept full Xray JSON or a vless:// share link (like .ovpn for OpenVPN)."""
    text = _extract_config_input(config_text)
    if text.startswith("{"):
        return json.loads(text)

    if _VLESS_URI_PATTERN.match(text):
        return vless_uri_to_config(text)

    raise ValueError("Xray config must be JSON or a vless:// share link")


def _extract_config_input(config_text: str) -> str:
    for line in config_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return config_text.strip()


def vless_uri_to_config(uri: str) -> dict[str, Any]:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "vless":
        raise ValueError("Expected vless:// URI")

    user_id = unquote(parsed.username or "")
    if not user_id:
        raise ValueError("Invalid vless URI: missing UUID")

    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        raise ValueError("Invalid vless URI: missing host or port")

    params = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
    encryption = params.get("encryption", "none")
    network = params.get("type", "tcp")
    security = params.get("security", "none")

    user: dict[str, Any] = {"id": user_id, "encryption": encryption}
    if flow := params.get("flow"):
        user["flow"] = flow

    outbound: dict[str, Any] = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": host,
                    "port": port,
                    "users": [user],
                }
            ]
        },
        "streamSettings": _build_stream_settings(network, security, host, params),
    }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": DEFAULT_SOCKS_LISTEN,
                "port": DEFAULT_SOCKS_PORT,
                "protocol": "socks",
                "settings": {"udp": True},
            }
        ],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom"},
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["socks-in"],
                    "outboundTag": "proxy",
                }
            ]
        },
    }


def _build_stream_settings(
    network: str,
    security: str,
    host: str,
    params: dict[str, str],
) -> dict[str, Any]:
    stream: dict[str, Any] = {"network": network}

    if security and security != "none":
        stream["security"] = security

    if security == "tls":
        tls_settings: dict[str, Any] = {}
        if sni := params.get("sni"):
            tls_settings["serverName"] = sni
        elif host:
            tls_settings["serverName"] = host
        if fp := params.get("fp"):
            tls_settings["fingerprint"] = fp
        if alpn := params.get("alpn"):
            tls_settings["alpn"] = [part.strip() for part in alpn.split(",") if part.strip()]
        if params.get("allowInsecure", "").lower() in {"1", "true", "yes"}:
            tls_settings["allowInsecure"] = True
        if tls_settings:
            stream["tlsSettings"] = tls_settings

    if security == "reality":
        reality_settings: dict[str, Any] = {}
        if sni := params.get("sni"):
            reality_settings["serverName"] = sni
        if pbk := params.get("pbk"):
            reality_settings["publicKey"] = pbk
        if sid := params.get("sid"):
            reality_settings["shortId"] = sid
        if fp := params.get("fp"):
            reality_settings["fingerprint"] = fp
        if spx := params.get("spx"):
            reality_settings["spiderX"] = spx
        if reality_settings:
            stream["realitySettings"] = reality_settings

    if network == "tcp":
        header_type = params.get("headerType", "none")
        if header_type != "none":
            stream["tcpSettings"] = {"header": {"type": header_type}}

    if network == "ws":
        ws_settings: dict[str, Any] = {"path": params.get("path", "/")}
        if host_header := params.get("host"):
            ws_settings["headers"] = {"Host": host_header}
        stream["wsSettings"] = ws_settings

    if network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": params.get("serviceName", params.get("path", "")),
            "multiMode": params.get("mode") == "multi",
        }

    if network in {"httpupgrade", "xhttp"}:
        stream[f"{network}Settings"] = {
            "path": params.get("path", "/"),
            "host": params.get("host", host),
        }

    return stream
