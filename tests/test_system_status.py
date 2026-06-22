from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_kind(client: TestClient, slug: str = "api") -> dict:
    response = client.post(
        "/api/admin/component-kinds",
        json={"name": "APIs", "slug": slug, "description": None},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_project(client: TestClient, slug: str = "timeline-demo") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "Timeline Demo", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


def _create_component(client: TestClient, *, project_id: str, kind_id: str, slug: str, name: str) -> dict:
    response = client.post(
        "/api/admin/monitored-components",
        json={
            "project_id": project_id,
            "component_kind_id": kind_id,
            "name": name,
            "slug": slug,
            "description": None,
            "environment": "prod",
            "check_url": "https://example.com/health",
            "check_method": "GET",
            "expected_status_code": 200,
            "timeout_seconds": 10,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return _data(response)


class TestPublicSystemStatus:
    def test_system_status_timeline(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client)
        project = _create_project(client)
        component = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="backend",
            name="Backend API",
        )

        good_day = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        bad_day = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=good_day,
                outcome=CheckOutcome.UP.value,
                latency_ms=42,
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=bad_day,
                outcome=CheckOutcome.DEGRADED.value,
                latency_ms=120,
            )
        )
        db_session.commit()

        client.post(
            f"/api/admin/projects/{project['id']}/incidents",
            json={
                "title": "Partial outage",
                "message": "We are investigating degraded performance.",
                "status": "investigating",
                "posted_at": bad_day.isoformat().replace("+00:00", "Z"),
            },
        )

        response = client.get(
            "/api/status/projects/timeline-demo/system-status",
            params={"end": "2026-06-16", "days": 7},
        )
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["project_slug"] == "timeline-demo"
        assert body["range_label"] == "Jun 2026"
        assert len(body["groups"]) == 1
        group = body["groups"][0]
        assert group["name"] == "APIs"
        assert group["component_count"] == 1
        assert len(group["days"]) == 7

        day_by_date = {day["date"]: day for day in group["days"]}
        assert day_by_date["2026-06-15"]["status"] == "operational"
        assert "1 checks: 1 ok" in day_by_date["2026-06-15"]["tooltip"]
        assert day_by_date["2026-06-15"]["check_count"] == 1
        assert day_by_date["2026-06-15"]["downtime_seconds"] == 0
        assert day_by_date["2026-06-16"]["status"] == "operational"
        assert "1 checks" in day_by_date["2026-06-16"]["tooltip"]
        assert "1 incident" in day_by_date["2026-06-16"]["tooltip"]
        assert len(day_by_date["2026-06-16"]["incidents"]) == 1

        assert len(body["active_alerts"]) >= 1

    def test_single_failure_stays_operational_for_day(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client, slug="vpn-kind")
        project = _create_project(client, slug="vpn-uptime")
        component = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="norway",
            name="Norway VPN",
        )

        day = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        for minute in range(100):
            db_session.add(
                CheckResult(
                    monitored_component_id=component["id"],
                    checked_at=day + timedelta(minutes=minute),
                    outcome=CheckOutcome.UP.value,
                    latency_ms=40,
                )
            )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=day + timedelta(minutes=100),
                outcome=CheckOutcome.TIMEOUT.value,
                latency_ms=60000,
            )
        )
        db_session.commit()

        response = client.get(
            "/api/status/projects/vpn-uptime/system-status",
            params={"end": "2026-06-20", "days": 7},
        )
        assert response.status_code == 200, response.text
        service = _data(response)["groups"][0]["services"][0]
        day_bar = next(day for day in service["days"] if day["date"] == "2026-06-20")
        assert day_bar["status"] == "operational"
        assert day_bar["check_count"] == 101
        assert day_bar["failed_count"] == 1
        assert day_bar["availability_percent"] == 99.01
        assert day_bar["downtime_seconds"] == 37_200
        assert service["uptime_percent"] == 99.01

    def test_service_downtime_from_check_sequence(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client, slug="downtime-kind")
        project = _create_project(client, slug="downtime-demo")
        component = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="api",
            name="API",
        )

        day = datetime(2026, 6, 21, 0, 0, tzinfo=UTC)
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=day + timedelta(hours=10),
                outcome=CheckOutcome.UP.value,
                latency_ms=40,
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=day + timedelta(hours=10, minutes=5),
                outcome=CheckOutcome.DOWN.value,
                latency_ms=None,
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=day + timedelta(hours=10, minutes=35),
                outcome=CheckOutcome.UP.value,
                latency_ms=42,
            )
        )
        db_session.commit()

        response = client.get(
            "/api/status/projects/downtime-demo/system-status",
            params={"end": "2026-06-21", "days": 7},
        )
        assert response.status_code == 200, response.text
        service = _data(response)["groups"][0]["services"][0]
        day_bar = next(day for day in service["days"] if day["date"] == "2026-06-21")
        assert day_bar["downtime_seconds"] == 30 * 60
        assert day_bar["check_count"] == 3

    def test_overnight_outage_carries_into_next_day(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kind = _create_kind(client, slug="overnight-kind")
        project = _create_project(client, slug="overnight-demo")
        component = _create_component(
            client,
            project_id=project["id"],
            kind_id=kind["id"],
            slug="api",
            name="API",
        )

        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=datetime(2026, 6, 20, 23, 0, tzinfo=UTC),
                outcome=CheckOutcome.DOWN.value,
                latency_ms=None,
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=datetime(2026, 6, 21, 1, 0, tzinfo=UTC),
                outcome=CheckOutcome.UP.value,
                latency_ms=40,
            )
        )
        db_session.commit()

        response = client.get(
            "/api/status/projects/overnight-demo/system-status",
            params={"end": "2026-06-21", "days": 7},
        )
        assert response.status_code == 200, response.text
        service = _data(response)["groups"][0]["services"][0]
        day_bar = next(day for day in service["days"] if day["date"] == "2026-06-21")
        assert day_bar["downtime_seconds"] == 3600

    def test_inactive_project_system_status_hidden(self, client: TestClient, admin_headers: dict) -> None:
        project = client.post(
            "/api/admin/projects",
            json={"name": "Hidden", "slug": "hidden-timeline", "description": None, "is_active": False},
        ).json()["data"]
        assert project["slug"] == "hidden-timeline"

        response = client.get("/api/status/projects/hidden-timeline/system-status")
        assert response.status_code == 404

    def test_public_project_status_includes_network_summary(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")
        project = _create_project(client, slug="vpn-status")
        component = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Norway OpenVPN",
                "slug": "norway-openvpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
                "timeout_seconds": 30,
            },
        ).json()["data"]

        checked_at = datetime.now(UTC)
        db_session.add(
            CheckResult(
                monitored_component_id=component["id"],
                checked_at=checked_at,
                outcome=CheckOutcome.UP.value,
                latency_ms=1500,
                details={
                    "check_type": "openvpn",
                    "network": {
                        "interface": "tun0",
                        "ipv4_address": "10.8.0.2",
                        "gateway": "10.8.0.1",
                        "dns_servers": ["1.1.1.1"],
                        "connect_time_ms": 900,
                        "probe": {
                            "url": "https://ifconfig.me/ip",
                            "exit_ip": "203.0.113.1",
                            "latency_ms": 80,
                            "ok": True,
                            "status_code": 200,
                        },
                    },
                },
            )
        )
        db_session.commit()

        response = client.get("/api/status/projects/vpn-status")
        assert response.status_code == 200, response.text
        body = _data(response)
        service = next(item for item in body["services"] if item["slug"] == "norway-openvpn")
        assert service["status"] == "up"
        assert service["network_summary"]["interface"] == "tun0"
        assert service["network_summary"]["ipv4_address"] == "10.8.0.2"
        assert service["network_summary"]["exit_ip"] == "203.0.113.1"
