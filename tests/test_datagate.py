"""Unit tests for DataGate matcher and client helpers (no live API)."""

from __future__ import annotations

import base64
from uuid import uuid4

import httpx

from app.services.datagate.client import DataGateClient, DataGateServer, parse_ovpn_endpoint
from app.services.datagate.matcher import (
    LocalVpnComponent,
    match_servers,
    monitor_common_name,
    normalize_name,
    score_pair,
)


def test_normalize_name_strips_emoji_and_tokens():
    assert normalize_name("🇳🇴 Norway 1 openvpn") == "norway 1"
    assert normalize_name("Helsinki 1 openvpn tcp") == "helsinki 1"


def test_monitor_common_name():
    assert monitor_common_name("statusgate", "datagate", 12) == "statusgate-datagate-12"


def test_parse_ovpn_endpoint():
    text = "client\nproto tcp\nremote vpn.example.com 443\n"
    assert parse_ovpn_endpoint(text) == {"host": "vpn.example.com", "port": 443, "proto": "tcp"}


def test_score_pair_matches_name_type_proto_and_host():
    server = DataGateServer(
        id=1,
        server_type=0,
        server_name="Norway 1",
        host="vpn.example.com",
        port=443,
        proto="tcp",
    )
    component = LocalVpnComponent(
        id=uuid4(),
        name="🇳🇴 Norway 1 openvpn",
        slug="norway-1-openvpn",
        check_type="openvpn",
        config_text="client\nproto tcp\nremote vpn.example.com 443\n",
    )
    score, endpoint_match = score_pair(server, component)
    assert endpoint_match is True
    assert score >= 40


def test_score_pair_rejects_type_mismatch():
    server = DataGateServer(id=1, server_type=1, server_name="Norway 1 xray")
    component = LocalVpnComponent(
        id=uuid4(),
        name="Norway 1 openvpn",
        slug="norway-1",
        check_type="openvpn",
    )
    score, _ = score_pair(server, component)
    assert score == 0


def test_match_servers_prefers_already_linked():
    sid = uuid4()
    server = DataGateServer(id=7, server_type=0, server_name="Cyprus", host="cy.example.com", proto="udp")
    other = DataGateServer(id=8, server_type=0, server_name="Cyprus", host="cy.example.com", proto="udp")
    linked = LocalVpnComponent(
        id=sid,
        name="Old Cyprus Name",
        slug="cyprus",
        check_type="openvpn",
        datagate_server_id=7,
        config_text="proto udp\nremote cy.example.com 1194\n",
    )
    buckets = match_servers([server, other], [linked])
    assert len(buckets.matched) == 1
    assert buckets.matched[0].already_linked is True
    assert buckets.matched[0].server.id == 7
    assert buckets.matched[0].name_differs is True
    assert len(buckets.new_servers) == 1
    assert buckets.new_servers[0].id == 8


def test_match_servers_fuzzy_by_host_and_name():
    server = DataGateServer(
        id=3,
        server_type=0,
        server_name="Helsinki 1",
        host="hel.example.com",
        port=443,
        proto="tcp",
    )
    component = LocalVpnComponent(
        id=uuid4(),
        name="Helsinki 1 openvpn tcp",
        slug="helsinki-3-openvpn",
        check_type="openvpn",
        config_text="proto tcp\nremote hel.example.com 443\n",
    )
    buckets = match_servers([server], [component])
    assert len(buckets.matched) == 1
    assert buckets.matched[0].component.slug == "helsinki-3-openvpn"
    assert buckets.new_servers == []


def test_datagate_client_token_and_list_servers():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/token":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "message": "Success",
                    "data": {"token": "tok-1", "expiration": "2099-01-01T00:00:00+00:00"},
                },
            )
        if request.url.path == "/api/v3/open-vpn-servers/get-all":
            assert request.headers.get("Authorization") == "Bearer tok-1"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "message": "Success",
                    "data": {
                        "vpnServers": [
                            {
                                "id": 1,
                                "serverType": 0,
                                "serverName": "Norway 1",
                                "apiUrl": "https://n1.example.com:9443/",
                                "isOnline": True,
                                "isDisabled": False,
                                "tags": ["tcp"],
                            }
                        ]
                    },
                },
            )
        return httpx.Response(404, json={"success": False, "message": "not found"})

    transport = httpx.MockTransport(handler)
    client = DataGateClient(
        "https://api.example.com",
        "cid",
        "secret",
        transport=transport,
    )
    servers = client.list_servers()
    assert len(servers) == 1
    assert servers[0].server_name == "Norway 1"
    assert servers[0].check_type == "openvpn"
    assert servers[0].host == "n1.example.com"


def test_datagate_client_download_decodes_base64():
    payload = b"client\nproto tcp\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/token":
            return httpx.Response(
                200,
                json={"success": True, "message": "ok", "data": {"token": "t", "expiration": "2099-01-01T00:00:00Z"}},
            )
        if request.url.path.endswith("/download-file-by-cn"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "message": "ok",
                    "data": {"content": base64.b64encode(payload).decode("ascii"), "fileSizeBytes": len(payload)},
                },
            )
        return httpx.Response(404, json={"success": False, "message": "missing"})

    client = DataGateClient("https://api.example.com", "c", "s", transport=httpx.MockTransport(handler))
    text = client.download_by_cn(vpn_server_id=1, common_name="statusgate-demo-1", xray=False)
    assert "proto tcp" in text
