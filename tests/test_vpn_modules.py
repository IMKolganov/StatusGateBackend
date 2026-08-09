"""Live Settings getters and TUN / OpenVPN / supervisor unit coverage."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core import probe_defaults
from app.core import speed_test_defaults
from app.core.probe_defaults import (
    FALLBACK_PROBE_URL,
    default_probe_url,
    google_probe_url,
    internet_ping_host,
)
from app.core.speed_test_defaults import (
    FALLBACK_CLOUDFLARE_SPEED_TEST_ORIGIN,
    FALLBACK_SPEED_TEST_URL_TEMPLATE,
    cloudflare_speed_test_origin,
    default_speed_test_url_template,
)
from app.models.enums import CheckOutcome, CheckType, ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.services import openvpn_session
from app.services import tun_iface
from app.services.speed_test_config import SpeedTestRunContext
from app.services.vpn_session_supervisor import (
    VpnSessionSupervisor,
    _PersistentOpenVpnWorker,
    _component_fingerprint,
    _connection_event_details,
)


def test_probe_defaults_follow_settings_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(probe_defaults.settings, "default_probe_url", "https://probe.example/ip")
    monkeypatch.setattr(probe_defaults.settings, "google_probe_url", "https://g.example/204")
    monkeypatch.setattr(probe_defaults.settings, "internet_ping_host", "9.9.9.9")
    assert default_probe_url() == "https://probe.example/ip"
    assert google_probe_url() == "https://g.example/204"
    assert internet_ping_host() == "9.9.9.9"


def test_probe_defaults_blank_settings_use_fallback(monkeypatch) -> None:
    monkeypatch.setattr(probe_defaults.settings, "default_probe_url", "  ")
    assert default_probe_url() == FALLBACK_PROBE_URL


def test_speed_defaults_follow_settings_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(
        speed_test_defaults.settings,
        "default_speed_test_url_template",
        "https://cdn.example/dl?bytes={bytes}",
    )
    monkeypatch.setattr(
        speed_test_defaults.settings,
        "cloudflare_speed_test_origin",
        "https://cdn.example/",
    )
    assert default_speed_test_url_template() == "https://cdn.example/dl?bytes={bytes}"
    assert cloudflare_speed_test_origin() == "https://cdn.example"


def test_speed_defaults_blank_settings_use_fallback(monkeypatch) -> None:
    monkeypatch.setattr(speed_test_defaults.settings, "default_speed_test_url_template", "")
    monkeypatch.setattr(speed_test_defaults.settings, "cloudflare_speed_test_origin", "")
    assert default_speed_test_url_template() == FALLBACK_SPEED_TEST_URL_TEMPLATE
    assert cloudflare_speed_test_origin() == FALLBACK_CLOUDFLARE_SPEED_TEST_ORIGIN


def test_tun_list_filters_tun_names() -> None:
    payload = '[{"ifname":"eth0"},{"ifname":"tun0"},{"ifname":"tun-abc"},{"ifname":"wlan0"}]'
    with patch("app.services.tun_iface.subprocess.check_output", return_value=payload):
        assert tun_iface._list_tun_interfaces() == ["tun0", "tun-abc"]


def test_tun_interface_requires_up_and_inet() -> None:
    down = '[{"flags":["LOWER_UP"],"addr_info":[{"family":"inet","local":"10.8.0.2"}]}]'
    with patch("app.services.tun_iface.subprocess.check_output", return_value=down):
        assert tun_iface._interface_is_up("tun0") is False

    up = '[{"flags":["UP","LOWER_UP"],"addr_info":[{"family":"inet","local":"10.8.0.2"}]}]'
    with patch("app.services.tun_iface.subprocess.check_output", return_value=up):
        assert tun_iface._interface_is_up("tun0") is True


def test_tun_peer_gateway_and_log_fallback() -> None:
    peer = '[{"addr_info":[{"family":"inet","local":"10.8.0.2","peer":"10.8.0.1/32"}]}]'
    with patch("app.services.tun_iface.subprocess.check_output", return_value=peer):
        assert tun_iface._tun_peer_gateway("tun0") == "10.8.0.1"

    assert (
        tun_iface._gateway_from_openvpn_log("PUSH_REPLY,route-gateway 10.51.15.1,redirect-gateway")
        == "10.51.15.1"
    )
    assert tun_iface._gateway_from_openvpn_log("net_addr_v4_add: 10.8.0.6/24 dev tun0") == "10.8.0.1"


def test_wait_for_tun_interface_returns_matching_device(monkeypatch) -> None:
    monkeypatch.setattr(tun_iface.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tun_iface.time, "time", MagicMock(side_effect=[0.0, 0.1, 0.2]))
    with patch("app.services.tun_iface._list_tun_interfaces", return_value=["tun-a", "tun-b"]):
        with patch(
            "app.services.tun_iface._interface_is_up",
            side_effect=lambda name, netns=None: name == "tun-b",
        ):
            assert tun_iface._wait_for_tun_interface(1.0, device="tun-b") == "tun-b"


def test_openvpn_config_and_bytes_helpers() -> None:
    component = MagicMock()
    component.check_config = {"config_text": "client\ndev tun\n"}
    component.speed_test_bytes = 1_048_576
    assert openvpn_session._config_text(component).startswith("client")
    assert openvpn_session._speed_test_bytes_for(component) == 1_048_576

    missing = MagicMock()
    missing.check_config = None
    with pytest.raises(ValueError, match="Missing VPN config"):
        openvpn_session._config_text(missing)


def test_connection_event_details_extracts_probe_only() -> None:
    assert _connection_event_details(None) is None
    assert _connection_event_details({"network": "bad"}) is None
    assert _connection_event_details({"network": {"probe": {"exit_ip": "1.2.3.4"}}}) == {
        "probe": {"exit_ip": "1.2.3.4"}
    }


def test_component_fingerprint_changes_with_config() -> None:
    a = MagicMock()
    a.check_config = {"config_text": "remote a\n"}
    a.check_url = ""
    a.timeout_seconds = 30
    a.speed_test_bytes = None
    a.speed_test_url_template = None
    a.speed_test_interval_seconds = None
    a.speed_test_enabled = True
    a.connection_mode = ConnectionMode.PERSISTENT.value
    b = MagicMock()
    b.check_config = {"config_text": "remote b\n"}
    b.check_url = ""
    b.timeout_seconds = 30
    b.speed_test_bytes = None
    b.speed_test_url_template = None
    b.speed_test_interval_seconds = None
    b.speed_test_enabled = True
    b.connection_mode = ConnectionMode.PERSISTENT.value
    assert _component_fingerprint(a) != _component_fingerprint(b)


def test_supervisor_should_run_only_persistent_openvpn() -> None:
    component = MagicMock()
    component.is_active = True
    component.check_type = "openvpn"
    component.connection_mode = ConnectionMode.PERSISTENT.value
    assert _PersistentOpenVpnWorker._should_run(component) is True
    component.connection_mode = ConnectionMode.EPHEMERAL.value
    assert _PersistentOpenVpnWorker._should_run(component) is False
    component.connection_mode = ConnectionMode.PERSISTENT.value
    component.is_active = False
    assert _PersistentOpenVpnWorker._should_run(component) is False


def test_supervisor_sync_stops_removed_workers() -> None:
    supervisor = VpnSessionSupervisor()
    component_id = uuid4()
    worker = MagicMock()
    stop_event = MagicMock()
    supervisor._workers[component_id] = worker
    supervisor._stop_events[component_id] = stop_event
    with patch.object(supervisor, "_load_desired_components", return_value=[]):
        supervisor.sync()
    stop_event.set.assert_called_once()
    worker.join.assert_called_once()
    assert supervisor._workers == {}


def _openvpn_component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "Persist VPN",
        "slug": "persist-vpn",
        "check_url": "https://ifconfig.me/ip",
        "check_method": "GET",
        "check_type": CheckType.OPENVPN.value,
        "check_config": {"config_text": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n"},
        "expected_status_code": 200,
        "timeout_seconds": 60,
        "connection_mode": ConnectionMode.PERSISTENT.value,
        "is_active": True,
        "speed_test_enabled": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


def test_start_openvpn_fails_when_tun_never_up() -> None:
    component = _openvpn_component()
    popen = MagicMock()
    popen.poll.return_value = None
    with patch("app.services.openvpn_session.ensure_netns"):
        with patch("app.services.openvpn_session.subprocess.Popen", return_value=popen):
            with patch("app.services.openvpn_session._wait_for_tun_interface", return_value=None):
                with patch("app.services.openvpn_session._read_tail", return_value="AUTH_FAILED"):
                    with patch("app.services.openvpn_session._terminate_process") as terminate:
                        with patch("app.services.openvpn_session.delete_netns") as delete:
                            with patch("app.services.openvpn_session._cleanup_persistent_tmpdir"):
                                result = openvpn_session.start_openvpn_persistent_session(component)
    assert result.handle is None
    assert result.error_message is not None
    assert "did not come up" in result.error_message
    terminate.assert_called_once()
    delete.assert_called_once()


def test_start_openvpn_fails_when_move_to_netns_raises() -> None:
    import subprocess

    component = _openvpn_component()
    short = str(component.id).split("-")[0]
    popen = MagicMock()
    popen.poll.return_value = None
    with patch("app.services.openvpn_session.ensure_netns"):
        with patch("app.services.openvpn_session.subprocess.Popen", return_value=popen):
            with patch("app.services.openvpn_session._wait_for_tun_interface", return_value=f"tun-{short}"):
                with patch("app.services.openvpn_session._tun_ipv4_addresses", return_value=[("10.8.0.2", 24)]):
                    with patch("app.services.openvpn_session._resolve_tun_gateway", return_value="10.8.0.1"):
                        with patch(
                            "app.services.openvpn_session.move_iface_to_netns",
                            side_effect=subprocess.CalledProcessError(1, ["ip", "link", "set"]),
                        ):
                            with patch("app.services.openvpn_session._read_tail", return_value="ok"):
                                with patch("app.services.openvpn_session._terminate_process"):
                                    with patch("app.services.openvpn_session.delete_netns"):
                                        with patch("app.services.openvpn_session._cleanup_persistent_tmpdir"):
                                            result = openvpn_session.start_openvpn_persistent_session(component)
    assert result.handle is None
    assert result.error_message is not None
    assert "Failed to move TUN" in result.error_message


def test_openvpn_session_lifecycle_and_successful_probe() -> None:
    component = _openvpn_component()
    short = str(component.id).split("-")[0]
    popen = MagicMock()
    popen.poll.return_value = None
    with patch("app.services.openvpn_session.ensure_netns"):
        with patch("app.services.openvpn_session.subprocess.Popen", return_value=popen):
            with patch("app.services.openvpn_session._wait_for_tun_interface", return_value=f"tun-{short}"):
                with patch("app.services.openvpn_session._tun_ipv4_addresses", return_value=[("10.8.0.2", 24)]):
                    with patch("app.services.openvpn_session._resolve_tun_gateway", return_value="10.8.0.1"):
                        with patch("app.services.openvpn_session.move_iface_to_netns"):
                            with patch("app.services.openvpn_session._interface_is_up", return_value=True):
                                with patch("app.services.openvpn_session._read_tail", return_value="route-gateway 10.8.0.1"):
                                    started = openvpn_session.start_openvpn_persistent_session(component)
    assert started.handle is not None
    handle = started.handle
    assert handle.iface == f"tun-{short}"
    with patch("app.services.openvpn_session._interface_is_up", return_value=True):
        assert openvpn_session.is_openvpn_persistent_session_up(handle) is True

    with patch("app.services.openvpn_session._resolve_tun_gateway", return_value="10.8.0.1") as resolve_gw:
        with patch("app.services.openvpn_session._read_tail", return_value="route-gateway 10.8.0.1"):
            assert openvpn_session.resolve_persistent_gateway(handle) == "10.8.0.1"
            resolve_gw.assert_called_once()

    context = SpeedTestRunContext.default()
    with patch("app.services.openvpn_session.is_openvpn_persistent_session_up", return_value=True):
        with patch(
            "app.services.openvpn_session._collect_network_details",
            return_value={"gateway": "10.8.0.1", "ipv4_address": "10.8.0.2"},
        ):
            with patch(
                "app.services.openvpn_session._probe_endpoint",
                return_value={"ok": True, "url": component.check_url, "status_code": 200, "exit_ip": "203.0.113.9"},
            ):
                with patch("app.services.openvpn_session._enrich_network_metrics") as enrich:
                    with patch("app.services.openvpn_session._read_tail", return_value=None):
                        result = openvpn_session.run_openvpn_persistent_probe(
                            component,
                            handle,
                            speed_test_context=context,
                            session_event="connected",
                        )
    assert result.outcome == CheckOutcome.UP.value
    assert result.details["session_event"] == "connected"
    assert result.details["network"]["probe"]["exit_ip"] == "203.0.113.9"
    enrich.assert_called_once()

    with patch("app.services.openvpn_session._terminate_process") as terminate:
        with patch("app.services.openvpn_session.delete_netns") as delete:
            with patch("app.services.openvpn_session._cleanup_persistent_tmpdir") as cleanup:
                openvpn_session.stop_openvpn_persistent_session(handle)
    terminate.assert_called_once()
    delete.assert_called_once_with(handle.netns)
    cleanup.assert_called_once_with(handle.tmpdir)


def test_openvpn_config_text_rejects_blank_config_text() -> None:
    component = MagicMock()
    component.check_config = {"config_text": "   "}
    with pytest.raises(ValueError, match="config_text is required"):
        openvpn_session._config_text(component)
