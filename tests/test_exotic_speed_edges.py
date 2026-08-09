"""Exotic / env / edge coverage for probe defaults and WAN overlay."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import probe_defaults
from app.services import host_wan_speed
from app.services import speed_measure
from app.services.host_wan_speed import (
    HostWanBaseline,
    attach_host_wan_baseline_to_network,
    ephemeral_openvpn_host_route_guard,
    get_latest_host_wan_baseline,
    reset_host_wan_state_for_tests,
    run_host_wan_speed_if_due,
)
from app.services.public_status_service import _build_tunnel_metric_point
from app.services.speed_test_config import (
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    estimate_speed_test_http_requests_per_minute,
    estimate_speed_tests_per_minute,
    is_cloudflare_speed_test_template,
    reset_cloudflare_speed_test_slot_for_tests,
    speed_test_rate_warning,
)
from tests.test_dual_path_speed import _settings
from tests.test_speed_test_settings import _vpn_component


def test_vpn_netns_nameserver_lines_parses_and_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(probe_defaults.settings, "vpn_netns_dns_servers", "9.9.9.9, 1.0.0.1")
    assert probe_defaults.vpn_netns_nameserver_lines() == "nameserver 9.9.9.9\nnameserver 1.0.0.1\n"

    monkeypatch.setattr(probe_defaults.settings, "vpn_netns_dns_servers", "  ,  ")
    assert probe_defaults.vpn_netns_nameserver_lines() == "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"


def test_is_cloudflare_speed_test_template_respects_origin(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.speed_test_defaults.settings.cloudflare_speed_test_origin",
        "https://speed.example.test",
    )
    assert is_cloudflare_speed_test_template("https://speed.example.test/__down?bytes={bytes}")
    assert not is_cloudflare_speed_test_template(DEFAULT_SPEED_TEST_URL_TEMPLATE)


def test_estimate_unbounded_uses_fastest_poll_among_unbounded() -> None:
    settings = _settings(default_poll_interval_seconds=300, default_speed_test_interval_seconds=3600)
    components = [
        _vpn_component(slug="slow", poll_interval_seconds=300, speed_test_interval_seconds=0),
        _vpn_component(slug="fast", poll_interval_seconds=60, speed_test_interval_seconds=0),
    ]
    # Shared slot paced by the fastest unbounded poller (60s), not components[0].
    assert estimate_speed_tests_per_minute(components, settings) == pytest.approx(1.0, rel=1e-6)


def test_estimate_http_requests_doubles_for_cloudflare_upload() -> None:
    settings = _settings(default_poll_interval_seconds=60, default_speed_test_interval_seconds=60)
    components = [_vpn_component(slug="vpn-a", poll_interval_seconds=60, speed_test_interval_seconds=60)]
    slots = estimate_speed_tests_per_minute(components, settings)
    http = estimate_speed_test_http_requests_per_minute(components, settings)
    assert slots == pytest.approx(1.0)
    assert http == pytest.approx(2.0)


def test_speed_test_rate_warning_mentions_http_requests() -> None:
    settings = _settings(default_poll_interval_seconds=60, default_speed_test_interval_seconds=60)
    components = [_vpn_component(slug=f"vpn-{i}", speed_test_interval_seconds=60) for i in range(12)]
    warning = speed_test_rate_warning(components, settings)
    assert warning is not None
    assert "HTTP requests" in warning


def test_pending_skip_reason_clears_after_successful_wan(tmp_path, monkeypatch) -> None:
    path = tmp_path / "wan.json"
    monkeypatch.setattr(host_wan_speed.settings, "host_wan_baseline_path", str(path))
    reset_host_wan_state_for_tests()
    reset_cloudflare_speed_test_slot_for_tests()

    prior = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    host_wan_speed._store_baseline(
        HostWanBaseline(
            measured_at=prior,
            download={"ok": True, "mbps": 100.0, "bytes": 1000, "duration_ms": 40, "measured_at": prior.isoformat()},
            upload=None,
        )
    )

    with ephemeral_openvpn_host_route_guard():
        run_host_wan_speed_if_due(_settings(default_speed_test_interval_seconds=1))

    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network.get("direct_speed_test_skip_reason") == "ephemeral_openvpn_active"
    assert network["direct_speed_test"]["mbps"] == 100.0

    monkeypatch.setattr(
        speed_measure,
        "measure_download_speed",
        lambda *a, **k: {"ok": True, "mbps": 150.0, "bytes": 1000, "duration_ms": 30, "url": "d"},
    )
    monkeypatch.setattr(
        speed_measure,
        "measure_upload_speed",
        lambda *a, **k: {"ok": True, "mbps": 40.0, "bytes": 1000, "duration_ms": 80, "url": "u"},
    )
    monkeypatch.setattr(host_wan_speed.time, "sleep", lambda *_: None)
    # Force due again.
    with host_wan_speed._store_lock:
        assert host_wan_speed._latest is not None
        host_wan_speed._latest = HostWanBaseline(
            measured_at=datetime.now(UTC) - timedelta(hours=2),
            download=host_wan_speed._latest.download,
            upload=host_wan_speed._latest.upload,
        )

    run_host_wan_speed_if_due(_settings(default_speed_test_interval_seconds=1))
    network2: dict = {}
    attach_host_wan_baseline_to_network(network2)
    assert "direct_speed_test_skip_reason" not in network2
    assert network2["direct_speed_test"]["mbps"] == 150.0


def test_tunnel_metric_point_falls_back_to_last_success() -> None:
    point = _build_tunnel_metric_point(
        checked_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        outcome="up",
        latency_ms=20,
        details={
            "network": {
                "speed_test": {"ok": False, "error": "429", "measured_at": "2026-08-09T12:00:00+00:00"},
                "speed_test_last_success": {
                    "ok": True,
                    "mbps": 88.0,
                    "bytes": 1000,
                    "duration_ms": 90,
                    "measured_at": "2026-08-09T11:00:00+00:00",
                },
                "speed_test_upload": {"ok": False, "error": "timeout"},
                "speed_test_upload_last_success": {
                    "ok": True,
                    "mbps": 12.0,
                    "bytes": 500,
                    "duration_ms": 300,
                    "measured_at": "2026-08-09T11:00:01+00:00",
                },
            }
        },
    )
    assert point.download_mbps == 88.0
    assert point.download_cached is True
    assert point.upload_mbps == 12.0
    assert point.upload_cached is True


def test_tunnel_metric_point_zero_mbps_live_falls_back() -> None:
    point = _build_tunnel_metric_point(
        checked_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        outcome="up",
        latency_ms=None,
        details={
            "network": {
                "speed_test": {"ok": True, "mbps": 0.0, "bytes": 0, "measured_at": "2026-08-09T12:00:00+00:00"},
                "speed_test_last_success": {
                    "ok": True,
                    "mbps": 50.0,
                    "bytes": 1000,
                    "duration_ms": 100,
                    "measured_at": "2026-08-09T10:00:00+00:00",
                },
            }
        },
    )
    assert point.download_mbps == 50.0
    assert point.download_cached is True


def test_corrupt_wan_baseline_file_is_ignored(tmp_path, monkeypatch) -> None:
    path = tmp_path / "wan.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(host_wan_speed.settings, "host_wan_baseline_path", str(path))
    reset_host_wan_state_for_tests()
    # reset deletes the file; recreate corrupt payload after reset.
    path.write_text("{not-json", encoding="utf-8")
    with host_wan_speed._store_lock:
        host_wan_speed._disk_hydrated = False
        host_wan_speed._disk_mtime_ns = None
    assert get_latest_host_wan_baseline() is None
