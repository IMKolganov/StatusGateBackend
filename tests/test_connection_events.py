"""Connection events timeline API and recording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.check_result import CheckResult
from app.models.connection_event import ConnectionEvent
from app.models.enums import CheckOutcome, ConnectionEventType, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.schemas.monitoring import ConnectionEventResponse
from app.services.connection_event_service import connection_event_label, record_connection_event
from app.services.monitoring_service import ConnectionEventRepository
from app.services.vpn_session_supervisor import _PersistentOpenVpnWorker


def _vpn_component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "Norway VPN",
        "slug": "norway-vpn",
        "check_url": "https://ifconfig.me/ip",
        "check_method": "GET",
        "check_type": "openvpn",
        "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
        "expected_status_code": 200,
        "timeout_seconds": 60,
        "connection_mode": ConnectionMode.PERSISTENT.value,
        "is_active": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


class TestConnectionEventService:
    def test_connection_event_labels(self) -> None:
        assert connection_event_label(ConnectionEventType.TUNNEL_UP.value) == "Connected"
        assert connection_event_label(ConnectionEventType.TUNNEL_DOWN.value) == "Disconnected"
        assert connection_event_label(ConnectionEventType.RECONNECT.value) == "Reconnecting"
        assert connection_event_label(ConnectionEventType.CONNECT_FAILED.value) == "Connect failed"
        assert connection_event_label(ConnectionEventType.UNAVAILABLE.value) == "Internet unavailable"
        assert connection_event_label(ConnectionEventType.AVAILABLE.value) == "Internet restored"
        assert connection_event_label("custom_event") == "Custom Event"

    def test_record_connection_event(self) -> None:
        session = MagicMock()
        component_id = uuid4()
        event = record_connection_event(
            session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        assert event.event_type == ConnectionEventType.TUNNEL_UP.value
        session.add.assert_called_once()
        session.flush.assert_called_once()

    def test_record_connection_event_rejects_unknown_type(self) -> None:
        session = MagicMock()
        with pytest.raises(ValueError, match="Unsupported connection event type"):
            record_connection_event(session, component_id=uuid4(), event_type="invalid")


class TestConnectionEventResponse:
    def test_model_validate_includes_event_label(self) -> None:
        event = ConnectionEvent(
            id=uuid4(),
            monitored_component_id=uuid4(),
            occurred_at=datetime.now(UTC),
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        response = ConnectionEventResponse.model_validate(event)
        assert response.event_label == "Connected"
        assert response.event_type == ConnectionEventType.TUNNEL_UP.value


class TestConnectionEventRepository:
    def test_list_for_component_paginated(self) -> None:
        session = MagicMock()
        component_id = uuid4()
        event = ConnectionEvent(
            id=uuid4(),
            monitored_component_id=component_id,
            occurred_at=datetime.now(UTC),
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        session.scalar.return_value = 1
        session.scalars.return_value.all.return_value = [event]

        repo = ConnectionEventRepository(session)
        items, total = repo.list_for_component_paginated(component_id, limit=10)

        assert total == 1
        assert len(items) == 1
        assert items[0].event_type == ConnectionEventType.TUNNEL_UP.value

    def test_purge_for_component(self, client: TestClient, admin_headers: dict, db_session) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-repo-purge")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")
        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Repo purge VPN",
                "slug": "repo-purge-vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        db_session.commit()

        repo = ConnectionEventRepository(db_session)
        deleted = repo.purge_for_component(component_id)
        db_session.commit()

        assert deleted == 1
        items, total = repo.list_for_component_paginated(component_id)
        assert total == 0
        assert items == []


class TestPersistentWorkerProbeTransitions:
    def test_records_unavailable_on_first_degraded_probe(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.DEGRADED.value,
            error_message="Probe failed",
            details={"network": {"probe": {"ok": False, "url": component.check_url}}},
        )

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.UNAVAILABLE.value
        assert worker._last_probe_ok is False

    def test_records_available_after_unavailable(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        worker._last_probe_ok = False
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={"network": {"probe": {"ok": True, "url": component.check_url}}},
        )

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.AVAILABLE.value
        assert worker._last_probe_ok is True

    def test_does_not_repeat_unavailable_while_still_degraded(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        worker._last_probe_ok = False
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.DEGRADED.value,
            error_message="Still down",
            details={"network": {"probe": {"ok": False}}},
        )

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_not_called()
        assert worker._last_probe_ok is False

    def test_ignores_non_probe_outcomes(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.DOWN.value,
            error_message="Tunnel down",
        )

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_not_called()
        assert worker._last_probe_ok is None

    def test_first_successful_probe_does_not_record_available(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={"network": {"probe": {"ok": True}}},
        )

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_not_called()
        assert worker._last_probe_ok is True

    def test_tunnel_down_preserves_probe_state_for_available_after_reconnect(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        worker._last_probe_ok = False
        component = _vpn_component()
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={"network": {"probe": {"ok": True}}},
        )

        assert worker._last_probe_ok is False

        with patch.object(worker, "_record_connection_event") as record:
            worker._maybe_record_probe_transition(component, result)

        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.AVAILABLE.value


class TestConnectionEventServiceDetails:
    def test_record_connection_event_persists_details(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session,
    ) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-details")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")
        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Details VPN",
                "slug": "details-vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        event = record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.UNAVAILABLE.value,
            outcome=CheckOutcome.DEGRADED.value,
            message="Probe failed",
            details={"probe": {"ok": False, "url": "https://example.com"}},
        )
        db_session.commit()

        repo = ConnectionEventRepository(db_session)
        items, total = repo.list_for_component_paginated(component_id)
        assert total == 1
        assert items[0].id == event.id
        assert items[0].details == {"probe": {"ok": False, "url": "https://example.com"}}


class TestPersistentWorkerSessionEvents:
    def test_save_connect_failure_records_event_and_check_result(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()

        with patch.object(worker, "_persist_result") as persist:
            with patch.object(worker, "_record_connection_event") as record:
                worker._save_connect_failure(component)

        persist.assert_called_once()
        result = persist.call_args.args[1]
        assert result.outcome == CheckOutcome.TIMEOUT.value
        assert result.details["session_event"] == ConnectionEventType.CONNECT_FAILED.value

        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.CONNECT_FAILED.value

    def test_save_session_event_without_handle_records_reconnect(self) -> None:
        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()

        with patch.object(worker, "_persist_result") as persist:
            with patch.object(worker, "_record_connection_event") as record:
                worker._save_session_event(component, None, ConnectionEventType.RECONNECT.value)

        persist.assert_called_once()
        result = persist.call_args.args[1]
        assert result.details["session_event"] == ConnectionEventType.RECONNECT.value
        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.RECONNECT.value

    def test_save_session_event_with_handle_records_tunnel_up(self) -> None:
        from app.services.vpn_check_service import OpenVpnSessionHandle

        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()
        handle = OpenVpnSessionHandle(
            component_id=component.id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=None)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(),
            pid_path=MagicMock(),
            connect_time_ms=100,
        )
        probe_result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={
                "session_event": ConnectionEventType.TUNNEL_UP.value,
                "network": {"probe": {"ok": True, "url": component.check_url}},
            },
        )

        with patch(
            "app.services.vpn_session_supervisor.run_openvpn_persistent_probe",
            return_value=probe_result,
        ):
            with patch.object(worker, "_persist_result") as persist:
                with patch.object(worker, "_record_connection_event") as record:
                    worker._save_session_event(component, handle, ConnectionEventType.TUNNEL_UP.value)

        persist.assert_called_once()
        result = persist.call_args.args[1]
        assert result.details["session_event"] == ConnectionEventType.TUNNEL_UP.value
        record.assert_called_once()
        assert record.call_args.args[1] == ConnectionEventType.TUNNEL_UP.value

    def test_reconnect_sequence_records_down_reconnect_up(self) -> None:
        from app.services.vpn_check_service import OpenVpnSessionHandle

        worker = _PersistentOpenVpnWorker(uuid4(), MagicMock())
        component = _vpn_component()
        handle = OpenVpnSessionHandle(
            component_id=component.id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=None)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(),
            pid_path=MagicMock(),
            connect_time_ms=100,
        )
        recorded: list[str] = []

        def capture(_component, event_type: str, **kwargs) -> None:
            recorded.append(event_type)

        probe_result = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={"session_event": ConnectionEventType.TUNNEL_UP.value},
        )

        with patch(
            "app.services.vpn_session_supervisor.run_openvpn_persistent_probe",
            return_value=probe_result,
        ):
            with patch.object(worker, "_persist_result"):
                with patch.object(worker, "_record_connection_event", side_effect=capture):
                    worker._save_session_event(component, handle, ConnectionEventType.TUNNEL_DOWN.value)
                    worker._save_session_event(component, None, ConnectionEventType.RECONNECT.value)
                    worker._save_session_event(component, handle, ConnectionEventType.TUNNEL_UP.value)

        assert recorded == [
            ConnectionEventType.TUNNEL_DOWN.value,
            ConnectionEventType.RECONNECT.value,
            ConnectionEventType.TUNNEL_UP.value,
        ]

    def test_shutdown_records_tunnel_down(self) -> None:
        from app.services.speed_test_config import SpeedTestRunContext
        from app.services.vpn_check_service import OpenVpnSessionHandle

        component_id = uuid4()
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False, True]
        stop_event.wait.return_value = True
        worker = _PersistentOpenVpnWorker(component_id, stop_event)
        component = _vpn_component(id=component_id)
        handle = OpenVpnSessionHandle(
            component_id=component_id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=None)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(),
            pid_path=MagicMock(),
            connect_time_ms=100,
        )
        probe_result = CheckResult(
            monitored_component_id=component_id,
            checked_at=datetime.now(UTC),
            outcome=CheckOutcome.UP.value,
            details={"network": {"probe": {"ok": True}}},
        )

        with patch.object(worker, "_load_component", return_value=component):
            with patch.object(worker, "_build_speed_test_context", return_value=SpeedTestRunContext.default()):
                with patch.object(worker, "_probe_interval_seconds", return_value=60):
                    with patch(
                        "app.services.vpn_session_supervisor.start_openvpn_persistent_session",
                        return_value=handle,
                    ):
                        with patch(
                            "app.services.vpn_session_supervisor.is_openvpn_persistent_session_up",
                            return_value=True,
                        ):
                            with patch(
                                "app.services.vpn_session_supervisor.run_openvpn_persistent_probe",
                                return_value=probe_result,
                            ):
                                with patch.object(worker, "_persist_result"):
                                    with patch.object(worker, "_record_connection_event") as record:
                                        with patch(
                                            "app.services.vpn_session_supervisor.stop_openvpn_persistent_session",
                                        ):
                                            worker.run()

        down_calls = [
            call
            for call in record.call_args_list
            if call.args[1] == ConnectionEventType.TUNNEL_DOWN.value
        ]
        assert len(down_calls) == 1
        assert down_calls[0].kwargs["message"] == "VPN session stopped"


class TestConnectionEventsApi:
    def test_list_connection_events_empty(self, client: TestClient, admin_headers: dict) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-project")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Events VPN",
                "slug": "events-vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        assert create.status_code == 201, create.text
        component_id = _data(create)["id"]

        response = client.get(f"/api/admin/monitoring/monitored-components/{component_id}/connection-events")
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["total"] == 0
        assert body["items"] == []

    def test_list_connection_events_with_data(self, client: TestClient, admin_headers: dict, db_session) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-project-2")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Events VPN 2",
                "slug": "events-vpn-2",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.RECONNECT.value,
            outcome=CheckOutcome.DOWN.value,
            message="Reconnecting",
        )
        db_session.commit()

        response = client.get(f"/api/admin/monitoring/monitored-components/{component_id}/connection-events")
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["total"] == 2
        assert body["items"][0]["event_label"] == "Reconnecting"
        assert body["items"][1]["event_label"] == "Connected"

    def test_list_connection_events_pagination(self, client: TestClient, admin_headers: dict, db_session) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-project-3")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Events VPN 3",
                "slug": "events-vpn-3",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]
        base = datetime.now(UTC)
        for idx, event_type in enumerate(
            [
                ConnectionEventType.TUNNEL_UP.value,
                ConnectionEventType.UNAVAILABLE.value,
                ConnectionEventType.AVAILABLE.value,
            ]
        ):
            record_connection_event(
                db_session,
                component_id=component_id,
                event_type=event_type,
                occurred_at=base + timedelta(minutes=idx),
                outcome=CheckOutcome.UP.value,
                message=event_type,
            )
        db_session.commit()

        response = client.get(
            f"/api/admin/monitoring/monitored-components/{component_id}/connection-events",
            params={"offset": 1, "limit": 1},
        )
        assert response.status_code == 200, response.text
        body = _data(response)
        assert body["total"] == 3
        assert len(body["items"]) == 1
        assert body["items"][0]["event_type"] == ConnectionEventType.UNAVAILABLE.value

    def test_list_connection_events_unknown_component(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get(f"/api/admin/monitoring/monitored-components/{uuid4()}/connection-events")
        assert response.status_code == 404

    def test_list_connection_events_requires_auth(self, client: TestClient) -> None:
        response = client.get(f"/api/admin/monitoring/monitored-components/{uuid4()}/connection-events")
        assert response.status_code == 401

    def test_delete_component_cascades_connection_events(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session,
    ) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-cascade")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")
        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Cascade VPN",
                "slug": "cascade-vpn",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        db_session.commit()

        delete = client.delete(f"/api/admin/monitored-components/{component_id}")
        assert delete.status_code == 200, delete.text

        remaining = db_session.scalar(
            select(func.count())
            .select_from(ConnectionEvent)
            .where(ConnectionEvent.monitored_component_id == component_id)
        )
        assert remaining == 0

    def test_purge_check_history_also_clears_connection_events(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session,
    ) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-purge")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Events VPN purge",
                "slug": "events-vpn-purge",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_DOWN.value,
            outcome=CheckOutcome.DOWN.value,
            message="Disconnected",
        )
        db_session.commit()

        purge = client.delete(f"/api/admin/monitoring/monitored-components/{component_id}/check-results")
        assert purge.status_code == 200, purge.text

        listed = client.get(f"/api/admin/monitoring/monitored-components/{component_id}/connection-events")
        assert listed.status_code == 200, listed.text
        assert _data(listed)["total"] == 0

    def test_purge_check_history_with_keep_preserves_connection_events(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session,
    ) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-purge-keep")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Events VPN purge keep",
                "slug": "events-vpn-purge-keep",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        record_connection_event(
            db_session,
            component_id=component_id,
            event_type=ConnectionEventType.TUNNEL_UP.value,
            outcome=CheckOutcome.UP.value,
            message="Connected",
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component_id,
                checked_at=datetime.now(UTC),
                outcome=CheckOutcome.UP.value,
                details={"session_event": "probe"},
            )
        )
        db_session.add(
            CheckResult(
                monitored_component_id=component_id,
                checked_at=datetime.now(UTC),
                outcome=CheckOutcome.UP.value,
                details={"session_event": "probe"},
            )
        )
        db_session.commit()

        purge = client.delete(
            f"/api/admin/monitoring/monitored-components/{component_id}/check-results",
            params={"keep": 1},
        )
        assert purge.status_code == 200, purge.text

        listed = client.get(f"/api/admin/monitoring/monitored-components/{component_id}/connection-events")
        assert listed.status_code == 200, listed.text
        assert _data(listed)["total"] == 1


class TestPersistentWorkerErrors:
    def test_exception_path_records_tunnel_down(self) -> None:
        from app.services.vpn_check_service import OpenVpnSessionHandle

        component_id = uuid4()
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False, True]
        stop_event.wait.return_value = True
        worker = _PersistentOpenVpnWorker(component_id, stop_event)
        component = _vpn_component(id=component_id)
        handle = OpenVpnSessionHandle(
            component_id=component_id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=None)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(),
            pid_path=MagicMock(),
            connect_time_ms=100,
        )

        with patch.object(worker, "_load_component", return_value=component):
            with patch(
                "app.services.vpn_session_supervisor.start_openvpn_persistent_session",
                return_value=handle,
            ):
                with patch(
                    "app.services.vpn_session_supervisor.is_openvpn_persistent_session_up",
                    return_value=True,
                ):
                    with patch.object(worker, "_save_session_event"):
                        with patch.object(worker, "_build_speed_test_context", side_effect=RuntimeError("boom")):
                            with patch.object(worker, "_record_connection_event") as record:
                                with patch("app.services.vpn_session_supervisor.stop_openvpn_persistent_session"):
                                    worker.run()

        down_calls = [
            call
            for call in record.call_args_list
            if call.args[1] == ConnectionEventType.TUNNEL_DOWN.value
        ]
        assert len(down_calls) == 1
        assert down_calls[0].kwargs["message"] == "VPN session stopped after worker error"


class TestPersistentManualCheck:
    def test_manual_check_rejected_for_persistent_openvpn(self, client: TestClient, admin_headers: dict) -> None:
        from tests.test_catalog import _create_project, _data

        project = _create_project(client, slug="events-manual-check")
        kinds = client.get("/api/admin/component-kinds")
        openvpn_kind = next(item for item in _data(kinds)["items"] if item["slug"] == "openvpn")

        create = client.post(
            "/api/admin/monitored-components",
            json={
                "project_id": project["id"],
                "component_kind_id": openvpn_kind["id"],
                "name": "Persistent manual",
                "slug": "persistent-manual",
                "check_type": "openvpn",
                "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
                "connection_mode": "persistent",
                "timeout_seconds": 30,
            },
        )
        component_id = _data(create)["id"]

        response = client.post(f"/api/admin/monitoring/monitored-components/{component_id}/check")
        assert response.status_code == 409, response.text

