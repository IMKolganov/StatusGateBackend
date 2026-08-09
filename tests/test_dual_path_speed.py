"""Dual-path speed metrics: VPN upload URL, enrich, and host WAN baseline."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.models.monitoring_settings import MonitoringSettings
from app.services import host_wan_speed
from app.services import network_enrich
from app.services import speed_measure
from app.services import vpn_check_service as vpn
from app.services.host_wan_speed import (
    HostWanBaseline,
    attach_host_wan_baseline_to_network,
    ephemeral_openvpn_host_route_guard,
    get_latest_host_wan_baseline,
    host_wan_baseline_at_or_before,
    reset_host_wan_state_for_tests,
    run_host_wan_speed_if_due,
)
from app.services.speed_test_config import (
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SpeedTestRunContext,
    build_speed_test_upload_url,
    reset_cloudflare_speed_test_slot_for_tests,
)


def _settings(**overrides) -> MonitoringSettings:
    base = {
        "id": uuid4(),
        "default_poll_interval_seconds": 60,
        "scheduler_interval_seconds": 30,
        "default_speed_test_url_template": DEFAULT_SPEED_TEST_URL_TEMPLATE,
        "default_speed_test_interval_seconds": 3600,
    }
    base.update(overrides)
    return MonitoringSettings(**base)


def test_build_speed_test_upload_url_from_cloudflare_template() -> None:
    url = build_speed_test_upload_url("https://speed.cloudflare.com/__down?bytes={bytes}")
    assert url == "https://speed.cloudflare.com/__up"


def test_build_speed_test_upload_url_rejects_custom_template() -> None:
    assert build_speed_test_upload_url("https://example.com/speed?bytes={bytes}") is None


def test_enrich_records_download_and_upload(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    network: dict = {}
    context = SpeedTestRunContext(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)

    def fake_download(url, *, proxy_url, timeout, netns=None):
        return {"ok": True, "url": url, "bytes": 1000, "duration_ms": 100, "mbps": 80.0}

    def fake_upload(url, *, bytes_count, proxy_url, timeout, netns=None):
        return {"ok": True, "url": url, "bytes": bytes_count, "duration_ms": 200, "mbps": 20.0}

    monkeypatch.setattr(speed_measure, "measure_download_speed", fake_download)
    monkeypatch.setattr(speed_measure, "measure_upload_speed", fake_upload)

    network_enrich._enrich_network_metrics(
        network,
        gateway=None,
        proxy_url=None,
        iface=None,
        timeout=10,
        speed_test_bytes=1000,
        speed_test_context=context,
    )

    assert network["speed_test"]["mbps"] == 80.0
    assert network["speed_test_upload"]["mbps"] == 20.0
    assert network["speed_test_stats"]["sample_count"] == 1
    assert network["speed_test_upload_stats"]["sample_count"] == 1


def test_enrich_demotes_zero_byte_upload(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    network: dict = {}
    context = SpeedTestRunContext(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)

    monkeypatch.setattr(
        speed_measure,
        "measure_download_speed",
        lambda *a, **k: {"ok": True, "url": "d", "bytes": 1000, "duration_ms": 50, "mbps": 10.0},
    )
    monkeypatch.setattr(
        speed_measure,
        "measure_upload_speed",
        lambda *a, **k: {"ok": True, "url": "u", "bytes": 0, "duration_ms": 50, "mbps": 0.0},
    )

    network_enrich._enrich_network_metrics(
        network,
        gateway=None,
        proxy_url=None,
        iface=None,
        timeout=10,
        speed_test_bytes=1000,
        speed_test_context=context,
    )
    assert network["speed_test_upload"]["ok"] is False
    assert "uploaded no data" in network["speed_test_upload"]["error"]


def test_enrich_throttled_preserves_upload_memory(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    network: dict = {}
    last_upload = {"ok": True, "mbps": 12.0, "bytes": 1000, "duration_ms": 200, "measured_at": "2026-08-09T10:00:00+00:00"}
    context = SpeedTestRunContext(
        url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE,
        run_speed_test=True,
        previous_speed_test={"ok": True, "mbps": 40.0, "bytes": 1000},
        last_successful_speed_test={"ok": True, "mbps": 40.0, "bytes": 1000},
        previous_speed_test_upload=last_upload,
        last_successful_speed_test_upload=last_upload,
        previous_speed_test_upload_stats={"min_mbps": 12.0, "max_mbps": 12.0, "avg_mbps": 12.0, "sample_count": 1},
    )
    monkeypatch.setattr(network_enrich, "try_acquire_speed_test_slot", lambda: False)

    network_enrich._enrich_network_metrics(
        network,
        gateway=None,
        proxy_url=None,
        iface=None,
        timeout=10,
        speed_test_context=context,
    )
    assert network["speed_test"]["cached"] is True
    assert network["speed_test_upload"]["mbps"] == 12.0
    assert network["speed_test_upload"]["deferred"] is True
    assert network["speed_test_upload"]["defer_reason"] == "slot"
    assert network["speed_test_upload_stats"]["sample_count"] == 1


def test_host_wan_skips_while_ephemeral_openvpn_active(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("must not measure while ephemeral is active")

    monkeypatch.setattr(speed_measure, "measure_download_speed", boom)
    monkeypatch.setattr(speed_measure, "measure_upload_speed", boom)

    with ephemeral_openvpn_host_route_guard():
        baseline = run_host_wan_speed_if_due(_settings())

    assert called["n"] == 0
    assert baseline is not None
    assert baseline.skipped is True
    assert baseline.skip_reason == "ephemeral_openvpn_active"


def test_host_wan_skip_keeps_prior_baseline_and_surfaces_reason(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    prior = HostWanBaseline(
        measured_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
        download={"ok": True, "mbps": 100.0, "bytes": 1000, "duration_ms": 80},
        upload={"ok": True, "mbps": 30.0, "bytes": 1000, "duration_ms": 200},
    )
    host_wan_speed._store_baseline(prior)

    with ephemeral_openvpn_host_route_guard():
        run_host_wan_speed_if_due(_settings())

    assert get_latest_host_wan_baseline() is prior
    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network["direct_speed_test"]["mbps"] == 100.0
    assert network["direct_speed_test_skip_reason"] == "ephemeral_openvpn_active"


def test_host_wan_respects_interval_without_remeasuring(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    called = {"n": 0}

    def count_download(*_a, **_k):
        called["n"] += 1
        return {"ok": True, "url": "d", "bytes": 1000, "duration_ms": 50, "mbps": 10.0}

    monkeypatch.setattr(speed_measure, "measure_download_speed", count_download)
    monkeypatch.setattr(
        speed_measure,
        "measure_upload_speed",
        lambda *a, **k: {"ok": True, "url": "u", "bytes": 1000, "duration_ms": 50, "mbps": 5.0},
    )
    monkeypatch.setattr(host_wan_speed.time, "sleep", lambda *_: None)

    first = run_host_wan_speed_if_due(_settings())
    second = run_host_wan_speed_if_due(_settings())
    assert called["n"] == 1
    assert second is first


def test_host_wan_slot_throttle_does_not_overwrite(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()
    prior = HostWanBaseline(
        measured_at=datetime(2026, 8, 1, tzinfo=UTC),
        download={"ok": True, "mbps": 55.0, "bytes": 1000, "duration_ms": 80},
        upload=None,
    )
    host_wan_speed._store_baseline(prior)
    monkeypatch.setattr(host_wan_speed, "try_acquire_speed_test_slot", lambda: False)
    monkeypatch.setattr(
        speed_measure,
        "measure_download_speed",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not measure")),
    )

    result = run_host_wan_speed_if_due(_settings(default_speed_test_interval_seconds=1))
    assert result is prior


def test_host_wan_baseline_at_or_before_ignores_future(monkeypatch) -> None:
    reset_host_wan_state_for_tests()
    future = HostWanBaseline(
        measured_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        download={"ok": True, "mbps": 90.0, "bytes": 1, "duration_ms": 1},
        upload=None,
    )
    host_wan_speed._store_baseline(future)
    assert host_wan_baseline_at_or_before(datetime(2026, 8, 9, 11, 0, tzinfo=UTC)) is None
    assert host_wan_baseline_at_or_before(datetime(2026, 8, 9, 13, 0, tzinfo=UTC)) is future


def test_host_wan_measures_and_attaches_to_network(monkeypatch) -> None:
    reset_cloudflare_speed_test_slot_for_tests()
    reset_host_wan_state_for_tests()

    monkeypatch.setattr(
        speed_measure,
        "measure_download_speed",
        lambda *a, **k: {"ok": True, "url": "d", "bytes": 1000, "duration_ms": 50, "mbps": 200.0},
    )
    monkeypatch.setattr(
        speed_measure,
        "measure_upload_speed",
        lambda *a, **k: {"ok": True, "url": "u", "bytes": 1000, "duration_ms": 100, "mbps": 50.0},
    )
    monkeypatch.setattr(host_wan_speed.time, "sleep", lambda *_: None)

    baseline = run_host_wan_speed_if_due(_settings())
    assert baseline is not None
    assert baseline.download is not None
    assert baseline.download["mbps"] == 200.0
    assert baseline.upload is not None
    assert baseline.upload["mbps"] == 50.0

    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network["direct_speed_test"]["mbps"] == 200.0
    assert network["direct_speed_test_upload"]["mbps"] == 50.0

    summary = vpn.public_network_summary({"network": network})
    assert summary is not None
    assert summary.direct_download_mbps == 200.0
    assert summary.direct_upload_mbps == 50.0


def test_public_network_summary_includes_upload_fields() -> None:
    summary = vpn.public_network_summary(
        {
            "network": {
                "speed_test": {
                    "ok": True,
                    "mbps": 90.0,
                    "bytes": 1000,
                    "duration_ms": 80,
                    "measured_at": "2026-08-09T12:00:00+00:00",
                },
                "speed_test_upload": {
                    "ok": True,
                    "mbps": 15.0,
                    "bytes": 1000,
                    "duration_ms": 500,
                    "measured_at": "2026-08-09T12:00:01+00:00",
                },
                "direct_speed_test": {
                    "ok": True,
                    "mbps": 300.0,
                    "bytes": 1000,
                    "duration_ms": 30,
                    "measured_at": "2026-08-09T11:00:00+00:00",
                },
                "direct_speed_test_upload": {
                    "ok": True,
                    "mbps": 40.0,
                    "bytes": 1000,
                    "duration_ms": 200,
                    "measured_at": "2026-08-09T11:00:01+00:00",
                },
            }
        }
    )
    assert summary is not None
    assert summary.download_mbps == 90.0
    assert summary.upload_mbps == 15.0
    assert summary.direct_download_mbps == 300.0
    assert summary.direct_upload_mbps == 40.0


def test_get_latest_after_reset_is_none() -> None:
    reset_host_wan_state_for_tests()
    assert get_latest_host_wan_baseline() is None


def test_host_wan_baseline_survives_memory_clear(tmp_path, monkeypatch) -> None:
    """API and worker are separate processes; disk must bridge the baseline."""
    path = tmp_path / "host_wan_baseline.json"
    monkeypatch.setattr(host_wan_speed.settings, "host_wan_baseline_path", str(path))
    reset_host_wan_state_for_tests()
    reset_cloudflare_speed_test_slot_for_tests()

    measured = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    host_wan_speed._store_baseline(
        HostWanBaseline(
            measured_at=measured,
            download={"ok": True, "mbps": 180.0, "bytes": 1000, "duration_ms": 40, "measured_at": measured.isoformat()},
            upload={"ok": True, "mbps": 35.0, "bytes": 1000, "duration_ms": 200, "measured_at": measured.isoformat()},
        )
    )
    assert path.is_file()

    # Simulate a fresh API process: empty memory, same shared file.
    with host_wan_speed._store_lock:
        host_wan_speed._latest = None
        host_wan_speed._history = []
        host_wan_speed._pending_skip_reason = None
        host_wan_speed._disk_hydrated = False
        host_wan_speed._disk_mtime_ns = None

    assert get_latest_host_wan_baseline() is not None
    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network["direct_speed_test"]["mbps"] == 180.0
    assert network["direct_speed_test_upload"]["mbps"] == 35.0


def test_host_wan_baseline_reloads_when_file_mtime_changes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "host_wan_baseline.json"
    monkeypatch.setattr(host_wan_speed.settings, "host_wan_baseline_path", str(path))
    reset_host_wan_state_for_tests()

    first = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    host_wan_speed._store_baseline(
        HostWanBaseline(
            measured_at=first,
            download={"ok": True, "mbps": 100.0, "bytes": 1000, "duration_ms": 50, "measured_at": first.isoformat()},
            upload=None,
        )
    )
    assert get_latest_host_wan_baseline() is not None
    assert get_latest_host_wan_baseline().download["mbps"] == 100.0

    # Another process writes a newer baseline to the shared file.
    second = datetime(2026, 8, 9, 13, 0, tzinfo=UTC)
    newer = {
        "latest": {
            "measured_at": second.isoformat(),
            "download": {
                "ok": True,
                "mbps": 250.0,
                "bytes": 1000,
                "duration_ms": 30,
                "measured_at": second.isoformat(),
            },
            "upload": None,
            "skipped": False,
            "skip_reason": None,
        },
        "history": [],
        "pending_skip_reason": None,
    }
    path.write_text(json.dumps(newer), encoding="utf-8")

    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network["direct_speed_test"]["mbps"] == 250.0


def test_failed_host_wan_run_keeps_previous_baseline(tmp_path, monkeypatch) -> None:
    path = tmp_path / "host_wan_baseline.json"
    monkeypatch.setattr(host_wan_speed.settings, "host_wan_baseline_path", str(path))
    reset_host_wan_state_for_tests()
    reset_cloudflare_speed_test_slot_for_tests()

    prior_at = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    host_wan_speed._store_baseline(
        HostWanBaseline(
            measured_at=prior_at,
            download={"ok": True, "mbps": 220.0, "bytes": 1000, "duration_ms": 40, "measured_at": prior_at.isoformat()},
            upload={"ok": True, "mbps": 55.0, "bytes": 1000, "duration_ms": 100, "measured_at": prior_at.isoformat()},
        )
    )

    monkeypatch.setattr(
        speed_measure,
        "measure_download_speed",
        lambda *a, **k: {"ok": False, "error": "timeout", "bytes": 0, "duration_ms": 1, "mbps": 0.0},
    )
    monkeypatch.setattr(
        speed_measure,
        "measure_upload_speed",
        lambda *a, **k: {"ok": False, "error": "timeout", "bytes": 0, "duration_ms": 1, "mbps": 0.0},
    )
    monkeypatch.setattr(host_wan_speed.time, "sleep", lambda *_: None)

    result = run_host_wan_speed_if_due(_settings(default_speed_test_interval_seconds=1))
    assert result is not None
    assert result.download["mbps"] == 220.0
    network: dict = {}
    attach_host_wan_baseline_to_network(network)
    assert network["direct_speed_test"]["mbps"] == 220.0


def test_format_speed_test_error_normalizes_curl_429() -> None:
    assert "429" in speed_measure.format_speed_test_error("curl: (22) The requested URL returned error: 429")
