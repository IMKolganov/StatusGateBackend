from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.connection_event import ConnectionEvent
from app.models.enums import CheckOutcome, ConnectionEventType


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_kind(client: TestClient, slug: str = "openvpn") -> dict:
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "OpenVPN", "slug": slug, "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_project(client: TestClient, slug: str = "tunnel-demo") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Tunnel Demo", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def test_tunnel_metrics_empty_window(client: TestClient, admin_headers: dict) -> None:
    kind = _create_kind(client, slug="openvpn-empty")
    project = _create_project(client, slug="tunnel-empty")
    component = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project["id"],
            "component_kind_id": kind["id"],
            "name": "Empty VPN",
            "slug": "empty-vpn",
            "check_type": "openvpn",
            "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
            "timeout_seconds": 30,
        },
    ).json()["data"]

    response = client.get(
        f"/api/status/projects/{project['slug']}/services/{component['slug']}/tunnel-metrics"
    )
    assert response.status_code == 200, response.text
    body = _data(response)
    assert body["service_slug"] == "empty-vpn"
    assert body["hours"] == 2
    assert body["points"] == []
    assert body["events"] == []


def test_tunnel_metrics_mixed_points_and_events(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
) -> None:
    kind = _create_kind(client, slug="openvpn-mixed")
    project = _create_project(client, slug="tunnel-mixed")
    component = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project["id"],
            "component_kind_id": kind["id"],
            "name": "Helsinki OpenVPN",
            "slug": "helsinki-openvpn",
            "check_type": "openvpn",
            "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
            "timeout_seconds": 30,
            "connection_mode": "persistent",
        },
    ).json()["data"]

    now = datetime.now(UTC)
    component_id = UUID(component["id"])
    db_session.add(
        CheckResult(
            monitored_component_id=component_id,
            checked_at=now - timedelta(minutes=90),
            outcome=CheckOutcome.UP.value,
            latency_ms=4000,
            details={
                "network": {
                    "gateway_ping": {"avg_ms": 34.5, "jitter_ms": 12.0, "loss_percent": 0.0},
                }
            },
        )
    )
    db_session.add(
        CheckResult(
            monitored_component_id=component_id,
            checked_at=now - timedelta(minutes=30),
            outcome=CheckOutcome.DOWN.value,
            latency_ms=6,
            details={"network": {"connect_time_ms": 511}},
            error_message="OpenVPN tunnel is down",
        )
    )
    db_session.add(
        CheckResult(
            monitored_component_id=component_id,
            checked_at=now - timedelta(minutes=5),
            outcome=CheckOutcome.UP.value,
            latency_ms=5200,
            details={
                "network": {
                    "gateway_ping": {"avg_ms": 41.2, "jitter_ms": 8.1, "loss_percent": 25.0},
                }
            },
        )
    )
    db_session.add(
        ConnectionEvent(
            monitored_component_id=component_id,
            occurred_at=now - timedelta(minutes=31),
            event_type=ConnectionEventType.TUNNEL_DOWN.value,
            outcome=CheckOutcome.DOWN.value,
            message="Disconnected",
        )
    )
    db_session.add(
        ConnectionEvent(
            monitored_component_id=component_id,
            occurred_at=now - timedelta(minutes=6),
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
    )
    # Outside the 2h window — must be ignored
    db_session.add(
        CheckResult(
            monitored_component_id=component_id,
            checked_at=now - timedelta(hours=5),
            outcome=CheckOutcome.UP.value,
            latency_ms=3000,
            details={"network": {"gateway_ping": {"avg_ms": 99.0, "jitter_ms": 1.0, "loss_percent": 0.0}}},
        )
    )
    db_session.commit()

    response = client.get(
        f"/api/status/projects/{project['slug']}/services/{component['slug']}/tunnel-metrics",
        params={"hours": 2},
    )
    assert response.status_code == 200, response.text
    body = _data(response)
    assert body["hours"] == 2
    assert len(body["points"]) == 3
    assert body["points"][0]["gateway_ping_avg_ms"] == 34.5
    assert body["points"][1]["outcome"] == "down"
    assert body["points"][1]["gateway_ping_avg_ms"] is None
    assert body["points"][2]["gateway_ping_loss_percent"] == 25.0
    assert [event["event_type"] for event in body["events"]] == ["tunnel_down", "tunnel_up"]


def test_tunnel_metrics_unknown_slug(client: TestClient, admin_headers: dict) -> None:
    kind = _create_kind(client, slug="openvpn-404")
    project = _create_project(client, slug="tunnel-404")
    client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project["id"],
            "component_kind_id": kind["id"],
            "name": "VPN",
            "slug": "real-vpn",
            "check_type": "openvpn",
            "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
            "timeout_seconds": 30,
        },
    )

    missing_project = client.get("/api/status/projects/no-such-project/services/real-vpn/tunnel-metrics")
    assert missing_project.status_code == 404

    missing_service = client.get(f"/api/status/projects/{project['slug']}/services/missing/tunnel-metrics")
    assert missing_service.status_code == 404
