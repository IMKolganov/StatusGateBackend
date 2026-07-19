"""Persistent OpenVPN connection mode: schemas, probes, supervisor, scheduling."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.enums import CheckOutcome, CheckType, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings
from app.schemas.monitored_component import MonitoredComponentCreate, MonitoredComponentUpdate
from app.schemas.network import VpnCheckConfig
from app.services import vpn_check_service as vpn
from app.services.monitoring_service import HealthCheckRunner
from app.services.speed_test_config import SpeedTestRunContext
from app.services.vpn_netns import ensure_netns, list_netns_names, netns_name_for_component
from app.services.vpn_session_supervisor import (
    VpnSessionSupervisor,
    _PersistentComponentSnapshot,
    _component_fingerprint,
)

_VPN_CONFIG = VpnCheckConfig(config_text="client\ndev tun\nproto udp\nremote vpn.example.com 1194\n")
_XRAY_CONFIG = VpnCheckConfig(config_text='{"inbounds":[{"protocol":"socks","port":1080}]}')


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
        "timeout_seconds": 60,
        "connection_mode": ConnectionMode.EPHEMERAL.value,
        "is_active": True,
        "speed_test_enabled": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


def _settings(**overrides) -> MonitoringSettings:
    base = {
        "id": MONITORING_SETTINGS_ID,
        "default_poll_interval_seconds": 60,
        "scheduler_interval_seconds": 30,
        "default_speed_test_url_template": "https://speed.cloudflare.com/__down?bytes={bytes}",
        "default_speed_test_interval_seconds": 3600,
    }
    base.update(overrides)
    return MonitoringSettings(**base)


@pytest.fixture(autouse=True)
def reset_supervisor() -> Generator[None, None, None]:
    VpnSessionSupervisor._instance = None
    yield
    if VpnSessionSupervisor._instance is not None:
        VpnSessionSupervisor.instance().stop_all()
    VpnSessionSupervisor._instance = None


class TestConnectionModeSchema:
    def test_create_defaults_to_ephemeral(self) -> None:
        payload = MonitoredComponentCreate(
            project_id=uuid4(),
            component_kind_id=uuid4(),
            name="VPN",
            slug="vpn",
            check_type="openvpn",
            check_config=_VPN_CONFIG,
        )
        assert payload.connection_mode == ConnectionMode.EPHEMERAL.value

    def test_create_persistent_openvpn_ok(self) -> None:
        payload = MonitoredComponentCreate(
            project_id=uuid4(),
            component_kind_id=uuid4(),
            name="VPN",
            slug="vpn",
            check_type="openvpn",
            check_config=_VPN_CONFIG,
            connection_mode=ConnectionMode.PERSISTENT.value,
        )
        assert payload.connection_mode == ConnectionMode.PERSISTENT.value

    def test_create_persistent_xray_rejected(self) -> None:
        with pytest.raises(ValidationError, match="persistent connection mode is only supported for openvpn"):
            MonitoredComponentCreate(
                project_id=uuid4(),
                component_kind_id=uuid4(),
                name="Xray",
                slug="xray",
                check_type="xray",
                check_config=_XRAY_CONFIG,
                connection_mode=ConnectionMode.PERSISTENT.value,
            )

    def test_create_persistent_on_http_rejected(self) -> None:
        with pytest.raises(ValidationError, match="connection_mode is only supported for VPN check types"):
            MonitoredComponentCreate(
                project_id=uuid4(),
                component_kind_id=uuid4(),
                name="Web",
                slug="web",
                check_url="https://example.com/health",
                connection_mode=ConnectionMode.PERSISTENT.value,
            )

    def test_update_persistent_xray_rejected(self) -> None:
        with pytest.raises(ValidationError, match="persistent connection mode is only supported for openvpn"):
            MonitoredComponentUpdate(
                check_type="xray",
                connection_mode=ConnectionMode.PERSISTENT.value,
            )


class TestHealthCheckRunnerPersistentSkip:
    def test_is_due_skips_persistent_openvpn(self) -> None:
        component = _vpn_component(connection_mode=ConnectionMode.PERSISTENT.value)
        runner = HealthCheckRunner(MagicMock())
        settings = _settings()
        assert runner.is_due(component, settings) is False

    def test_is_due_includes_ephemeral_openvpn(self) -> None:
        component = _vpn_component(last_checked_at=None)
        runner = HealthCheckRunner(MagicMock())
        settings = _settings()
        assert runner.is_due(component, settings) is True

    def test_is_due_persistent_even_when_interval_elapsed(self) -> None:
        component = _vpn_component(
            connection_mode=ConnectionMode.PERSISTENT.value,
            last_checked_at=datetime.now(UTC) - timedelta(hours=2),
        )
        runner = HealthCheckRunner(MagicMock())
        settings = _settings()
        assert runner.is_due(component, settings) is False


class TestVpnNetnsHelpers:
    def test_netns_name_for_component(self) -> None:
        component_id = uuid4()
        assert netns_name_for_component(component_id) == f"sg-{str(component_id).split('-')[0]}"

    def test_list_netns_names_parses_plain_output(self) -> None:
        with patch("app.services.vpn_netns.subprocess.check_output", side_effect=[FileNotFoundError, "sg-abc (id: 0)\n"]):
            assert list_netns_names() == {"sg-abc"}

    def test_ensure_netns_creates_and_brings_up_lo(self) -> None:
        calls: list[list[str]] = []

        def fake_check_call(cmd: list[str]) -> None:
            calls.append(cmd)

        with patch("app.services.vpn_netns.list_netns_names", return_value=set()):
            with patch("app.services.vpn_netns.subprocess.check_call", side_effect=fake_check_call):
                ensure_netns("sg-test")
        assert calls[0] == ["ip", "netns", "add", "sg-test"]
        assert calls[1] == ["ip", "netns", "exec", "sg-test", "ip", "link", "set", "lo", "up"]

    def test_ensure_netns_skips_existing(self) -> None:
        with patch("app.services.vpn_netns.list_netns_names", return_value={"sg-test"}):
            with patch("app.services.vpn_netns.subprocess.check_call") as check_call:
                ensure_netns("sg-test")
        check_call.assert_not_called()

    def test_ensure_netns_permission_error_is_actionable(self) -> None:
        from app.services.vpn_netns import NetnsPermissionError

        with patch("app.services.vpn_netns.list_netns_names", return_value=set()):
            with patch(
                "app.services.vpn_netns.subprocess.check_call",
                side_effect=subprocess.CalledProcessError(1, ["ip", "netns", "add", "sg-test"]),
            ):
                with pytest.raises(NetnsPermissionError, match="SYS_ADMIN"):
                    ensure_netns("sg-test")


class TestPersistentProbeHelpers:
    def test_probe_endpoint_via_curl_success(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="203.0.113.1\n__HTTP_CODE__:204",
            stderr="",
        )
        with patch("app.services.vpn_check_service.subprocess.run", return_value=completed):
            result = vpn._probe_endpoint_via_curl("https://www.google.com/generate_204", 5, netns="sg-test")
        assert result["ok"] is True
        assert result["status_code"] == 204
        assert result["exit_ip"] == "203.0.113.1"

    def test_probe_endpoint_via_curl_failure(self) -> None:
        completed = SimpleNamespace(returncode=28, stdout="", stderr="timeout")
        with patch("app.services.vpn_check_service.subprocess.run", return_value=completed):
            result = vpn._probe_endpoint_via_curl("https://example.com", 5, netns="sg-test")
        assert result["ok"] is False
        assert "timeout" in result["error"]

    def test_run_openvpn_persistent_probe_degraded_when_probe_fails(self) -> None:
        component = _vpn_component()
        handle = vpn.OpenVpnSessionHandle(
            component_id=component.id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=None)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(),
            pid_path=MagicMock(),
            connect_time_ms=900,
        )
        with patch.object(vpn, "is_openvpn_persistent_session_up", return_value=True):
            with patch.object(vpn, "_collect_network_details", return_value={"gateway": "10.8.0.1"}):
                with patch.object(
                    vpn,
                    "_probe_endpoint",
                    return_value={"ok": False, "url": component.check_url, "error": "timeout"},
                ):
                    result = vpn.run_openvpn_persistent_probe(
                        component,
                        handle,
                        speed_test_context=SpeedTestRunContext.default(),
                    )
        assert result.outcome == CheckOutcome.DEGRADED.value
        assert result.details["connection_mode"] == ConnectionMode.PERSISTENT.value
        assert result.details["session_event"] == "probe"

    def test_run_openvpn_persistent_probe_down_when_tunnel_down(self) -> None:
        component = _vpn_component()
        handle = vpn.OpenVpnSessionHandle(
            component_id=component.id,
            netns="sg-test",
            proc=MagicMock(poll=MagicMock(return_value=1)),
            iface="tun0",
            tmpdir="/tmp/test",
            config_path=MagicMock(),
            log_path=MagicMock(exists=MagicMock(return_value=False)),
            pid_path=MagicMock(),
            connect_time_ms=900,
        )
        with patch.object(vpn, "is_openvpn_persistent_session_up", return_value=False):
            result = vpn.run_openvpn_persistent_probe(
                component,
                handle,
                speed_test_context=SpeedTestRunContext.default(),
                session_event="tunnel_down",
            )
        assert result.outcome == CheckOutcome.DOWN.value
        assert result.details["session_event"] == "tunnel_down"


class TestVpnSessionSupervisor:
    def test_component_fingerprint_changes_when_config_changes(self) -> None:
        component = _vpn_component()
        first = _component_fingerprint(component)
        component.check_url = "https://www.google.com/generate_204"
        second = _component_fingerprint(component)
        assert first != second

    def test_sync_starts_worker_for_persistent_component(self) -> None:
        component_id = uuid4()
        with patch.object(VpnSessionSupervisor, "_load_desired_components", return_value=[component_id]):
            with patch.object(VpnSessionSupervisor, "_snapshot_for") as snapshot_for:
                snapshot_for.return_value = SimpleNamespace(
                    component_id=component_id,
                    config_fingerprint="abc",
                    poll_interval_seconds=60,
                    is_active=True,
                )
                with patch("app.services.vpn_session_supervisor._PersistentOpenVpnWorker") as worker_cls:
                    worker = MagicMock()
                    worker_cls.return_value = worker
                    supervisor = VpnSessionSupervisor.instance()
                    supervisor.sync()
                    worker_cls.assert_called_once_with(component_id, supervisor._stop_events[component_id])
                    worker.start.assert_called_once()

    def test_sync_stops_worker_when_component_removed(self) -> None:
        component_id = uuid4()
        supervisor = VpnSessionSupervisor.instance()
        stop_event = MagicMock()
        worker = MagicMock()
        supervisor._workers[component_id] = worker
        supervisor._stop_events[component_id] = stop_event
        supervisor._snapshots[component_id] = _PersistentComponentSnapshot(
            component_id=component_id,
            config_fingerprint="abc",
            poll_interval_seconds=60,
            is_active=True,
        )

        with patch.object(VpnSessionSupervisor, "_load_desired_components", return_value=[]):
            supervisor.sync()

        stop_event.set.assert_called_once()
        worker.join.assert_called_once_with(timeout=30)
        assert component_id not in supervisor._workers

    def test_sync_restarts_worker_when_fingerprint_changes(self) -> None:
        component_id = uuid4()
        supervisor = VpnSessionSupervisor.instance()
        old_stop = MagicMock()
        old_worker = MagicMock()
        supervisor._workers[component_id] = old_worker
        supervisor._stop_events[component_id] = old_stop
        supervisor._snapshots[component_id] = _PersistentComponentSnapshot(
            component_id=component_id,
            config_fingerprint="old",
            poll_interval_seconds=60,
            is_active=True,
        )

        with patch.object(VpnSessionSupervisor, "_load_desired_components", return_value=[component_id]):
            with patch.object(VpnSessionSupervisor, "_snapshot_for") as snapshot_for:
                snapshot_for.return_value = _PersistentComponentSnapshot(
                    component_id=component_id,
                    config_fingerprint="new",
                    poll_interval_seconds=60,
                    is_active=True,
                )
                with patch("app.services.vpn_session_supervisor._PersistentOpenVpnWorker") as worker_cls:
                    new_worker = MagicMock()
                    worker_cls.return_value = new_worker
                    supervisor.sync()
                    old_stop.set.assert_called_once()
                    old_worker.join.assert_called_once_with(timeout=30)
                    worker_cls.assert_called_once()
                    new_worker.start.assert_called_once()

    def test_worker_run_exits_when_component_deactivated(self) -> None:
        from app.services.vpn_session_supervisor import _PersistentOpenVpnWorker

        component_id = uuid4()
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False, True]
        worker = _PersistentOpenVpnWorker(component_id, stop_event)
        inactive = _vpn_component(id=component_id, is_active=False, connection_mode=ConnectionMode.PERSISTENT.value)

        with patch.object(worker, "_load_component", return_value=inactive):
            worker.run()

        stop_event.wait.assert_not_called()
