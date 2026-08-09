"""Exotic network_enrich / tunnel_ping_sampler / speed_measure edge cases."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx

from app.services import network_enrich
from app.services import speed_measure
from app.services.speed_test_config import DEFAULT_SPEED_TEST_URL_TEMPLATE, SpeedTestRunContext, reset_cloudflare_speed_test_slot_for_tests
from app.services.tunnel_ping_sampler import TunnelPingSampler, parse_ping_aggregate


class TestCollectNetworkDetailsEdges:
    def test_corrupt_json_addr_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setattr(
            network_enrich,
            "_read_dns_servers",
            lambda: ["1.1.1.1"],
        )

        def fake_check_output(cmd, **_kwargs):
            if "addr" in cmd:
                return "{not-json"
            if "route" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            if "link" in cmd:
                return "[]"
            raise AssertionError(cmd)

        monkeypatch.setattr(network_enrich.subprocess, "check_output", fake_check_output)
        details = network_enrich._collect_network_details("tun0")
        assert details["dns_servers"] == ["1.1.1.1"]
        assert details["ipv4_addresses"] == []
        assert details["routes"] == []
        assert "ipv4_address" not in details
        assert "mtu" not in details

    def test_partial_ip_json_parses_what_it_can(self, monkeypatch) -> None:
        monkeypatch.setattr(network_enrich, "_read_dns_servers", lambda: [])

        addr = [
            {
                "addr_info": [
                    {"family": "inet", "local": "10.8.0.2"},
                    {"family": "inet6", "local": "fd00::1"},
                    {"family": "inet", "local": None},
                    {"family": "inet"},
                ]
            }
        ]
        routes = [
            {"dst": "default", "gateway": "10.8.0.1", "prefsrc": "10.8.0.2"},
            {"dst": "10.8.0.0/24"},
        ]
        link = [{"mtu": 1400, "operstate": "UP"}]

        def fake_check_output(cmd, **_kwargs):
            if "addr" in cmd:
                return json.dumps(addr)
            if "route" in cmd:
                return json.dumps(routes)
            if "link" in cmd:
                return json.dumps(link)
            raise AssertionError(cmd)

        monkeypatch.setattr(network_enrich.subprocess, "check_output", fake_check_output)
        details = network_enrich._collect_network_details("tun0")
        assert details["ipv4_address"] == "10.8.0.2"
        assert details["ipv6_addresses"] == ["fd00::1"]
        assert details["gateway"] == "10.8.0.1"
        assert details["mtu"] == 1400
        assert details["operstate"] == "UP"
        assert len(details["routes"]) == 2

    def test_netns_path_uses_run_ip_command(self, monkeypatch) -> None:
        monkeypatch.setattr(network_enrich, "_read_dns_servers", lambda: [])
        calls: list[list[str]] = []

        def fake_run_ip(args, *, netns):
            calls.append([netns, *args])
            if "addr" in args:
                return json.dumps([{"addr_info": [{"family": "inet", "local": "10.9.0.2"}]}])
            if "route" in args:
                return "[]"
            if "link" in args:
                return json.dumps([{"mtu": 1500}])
            return "[]"

        monkeypatch.setattr(network_enrich, "run_ip_command", fake_run_ip)
        details = network_enrich._collect_network_details("tun0", netns="sg-test")
        assert details["ipv4_address"] == "10.9.0.2"
        assert details["mtu"] == 1500
        assert all(call[0] == "sg-test" for call in calls)


class TestPingHostEdges:
    def test_empty_ping_output_returns_none(self) -> None:
        assert network_enrich._parse_ping_output("") is None
        assert network_enrich._parse_ping_output("ping: Network unreachable") is None

    def test_ping_host_subprocess_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(
            network_enrich.subprocess,
            "check_output",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd="ping", timeout=1)),
        )
        assert network_enrich._ping_host("10.8.0.1") is None

    def test_ping_host_parses_loss_only(self, monkeypatch) -> None:
        output = "4 packets transmitted, 0 received, 100% packet loss, time 3000ms\n"
        monkeypatch.setattr(network_enrich.subprocess, "check_output", MagicMock(return_value=output))
        parsed = network_enrich._ping_host("8.8.8.8", count=4, timeout=2)
        assert parsed == {"loss_percent": 100.0}


class TestEnrichNetworkTimeoutAndZeroBytes:
    def test_enrich_gateway_ping_timeout_skips_field(self, monkeypatch) -> None:
        reset_cloudflare_speed_test_slot_for_tests()
        monkeypatch.setattr(network_enrich, "_ping_host", lambda *a, **k: None)
        monkeypatch.setattr(network_enrich, "try_acquire_speed_test_slot", lambda: False)
        network: dict = {}
        context = SpeedTestRunContext(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=False)
        network_enrich._enrich_network_metrics(
            network,
            gateway="10.8.0.1",
            proxy_url=None,
            iface=None,
            timeout=10,
            speed_test_context=context,
        )
        assert "gateway_ping" not in network
        assert network["speed_test"]["deferred"] is True

    def test_enrich_zero_byte_download_demoted(self, monkeypatch) -> None:
        reset_cloudflare_speed_test_slot_for_tests()
        network: dict = {}
        context = SpeedTestRunContext(url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE, run_speed_test=True)
        monkeypatch.setattr(
            speed_measure,
            "measure_download_speed",
            lambda *a, **k: {"ok": True, "url": "d", "bytes": 0, "duration_ms": 40, "mbps": 0.0},
        )
        monkeypatch.setattr(
            speed_measure,
            "measure_upload_speed",
            lambda *a, **k: {"ok": True, "url": "u", "bytes": 1000, "duration_ms": 50, "mbps": 5.0},
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
        assert network["speed_test"]["ok"] is False
        assert "no data" in network["speed_test"]["error"].lower()
        assert network["speed_test_upload"]["ok"] is True

    def test_enrich_measure_returns_none(self, monkeypatch) -> None:
        reset_cloudflare_speed_test_slot_for_tests()
        last = {"ok": True, "mbps": 40.0, "bytes": 1000, "measured_at": "2026-08-09T10:00:00+00:00"}
        network: dict = {}
        context = SpeedTestRunContext(
            url_template=DEFAULT_SPEED_TEST_URL_TEMPLATE,
            run_speed_test=True,
            last_successful_speed_test=last,
            previous_speed_test_stats={"min_mbps": 40.0, "max_mbps": 40.0, "avg_mbps": 40.0, "sample_count": 1},
        )
        monkeypatch.setattr(speed_measure, "measure_download_speed", lambda *a, **k: None)
        monkeypatch.setattr(speed_measure, "measure_upload_speed", lambda *a, **k: None)
        network_enrich._enrich_network_metrics(
            network,
            gateway=None,
            proxy_url=None,
            iface=None,
            timeout=10,
            speed_test_bytes=1000,
            speed_test_context=context,
        )
        assert network["speed_test"]["ok"] is False
        assert network["speed_test"]["error"] == "Speed test failed"
        assert network["speed_test_last_success"]["mbps"] == 40.0
        assert network["speed_test_stats"]["sample_count"] == 1


class TestSpeedMeasureEdges:
    def test_download_timeout(self, monkeypatch) -> None:
        class FakeClient:
            def stream(self, *_args, **_kwargs):
                raise httpx.TimeoutException("timed out")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(speed_measure.httpx, "Client", lambda **_k: FakeClient())
        result = speed_measure.measure_download_speed("https://example.test/down", proxy_url=None, timeout=1)
        assert result is not None
        assert result["ok"] is False
        assert result["error"] == "Speed test timed out"

    def test_download_zero_bytes(self, monkeypatch) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self):
                if False:
                    yield b""

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *_args):
                return False

        class FakeClient:
            def stream(self, *_args, **_kwargs):
                return FakeStream()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(speed_measure.httpx, "Client", lambda **_k: FakeClient())
        monkeypatch.setattr(speed_measure.time, "perf_counter", lambda: 0.0)
        result = speed_measure.measure_download_speed("https://example.test/down", proxy_url=None, timeout=1)
        assert result is not None
        assert result["ok"] is False
        assert result["bytes"] == 0
        assert "no data" in result["error"].lower()

    def test_upload_zero_payload(self, monkeypatch) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def post(self, *_args, **_kwargs):
                return FakeResponse()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(speed_measure.httpx, "Client", lambda **_k: FakeClient())
        monkeypatch.setattr(speed_measure.time, "perf_counter", lambda: 0.0)
        result = speed_measure.measure_upload_speed(
            "https://example.test/up",
            bytes_count=0,
            proxy_url=None,
            timeout=1,
        )
        assert result is not None
        assert result["ok"] is False
        assert result["bytes"] == 0

    def test_curl_download_timeout(self, monkeypatch) -> None:
        monkeypatch.setattr(
            speed_measure.subprocess,
            "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=1)),
        )
        result = speed_measure.measure_download_speed_curl(
            "https://example.test/down",
            timeout=1,
            netns="sg-test",
        )
        assert result is not None
        assert result["ok"] is False
        assert "timed out" in result["error"].lower() or "failed" in result["error"].lower()

    def test_curl_download_zero_bytes(self, monkeypatch) -> None:
        completed = MagicMock(returncode=0, stdout="0\n", stderr="")
        monkeypatch.setattr(speed_measure.subprocess, "run", MagicMock(return_value=completed))
        monkeypatch.setattr(speed_measure.time, "perf_counter", lambda: 0.0)
        result = speed_measure.measure_download_speed_curl(
            "https://example.test/down",
            timeout=5,
            netns="sg-test",
        )
        assert result is not None
        assert result["ok"] is False
        assert result["bytes"] == 0

    def test_format_speed_test_error_variants(self) -> None:
        assert speed_measure.format_speed_test_error(None) == "Speed test failed"
        assert speed_measure.format_speed_test_error("Speed test already ok") == "Speed test already ok"
        assert "403" in speed_measure.format_speed_test_error("Client error '403 Forbidden' for url 'x'")
        assert speed_measure.format_speed_test_error("connection timeout here") == "Speed test timed out"


class TestTunnelPingSamplerEdges:
    def test_parse_ping_aggregate_zero_sent(self) -> None:
        output = "0 packets transmitted, 0 received, 0% packet loss\n"
        assert parse_ping_aggregate(output) is None

    def test_targets_without_gateway(self) -> None:
        sampler = TunnelPingSampler(uuid4(), netns="sg-x", gateway=None)
        targets = sampler._targets()
        assert len(targets) == 1
        assert targets[0][0] == "internet"

    def test_targets_with_gateway(self) -> None:
        sampler = TunnelPingSampler(uuid4(), netns="sg-x", gateway="10.8.0.1")
        targets = sampler._targets()
        assert targets[0] == ("gateway", "10.8.0.1")
        assert targets[1][0] == "internet"

    def test_sample_window_skips_unparseable_output(self, monkeypatch) -> None:
        sampler = TunnelPingSampler(uuid4(), netns="sg-x", gateway="10.8.0.1")
        monkeypatch.setattr(sampler, "_targets", lambda: [("gateway", "10.8.0.1")])

        class FakeProc:
            def __init__(self) -> None:
                self._polled = False

            def poll(self):
                if self._polled:
                    return 0
                self._polled = True
                return None

            def send_signal(self, *_args):
                return None

            def communicate(self, timeout=None):
                return ("garbage", None)

            def kill(self):
                return None

        with patch("app.services.tunnel_ping_sampler.subprocess.Popen", return_value=FakeProc()):
            rows = sampler._sample_window(datetime.now(UTC))
        assert rows == []

    def test_sample_window_oserror_skips_target(self, monkeypatch) -> None:
        sampler = TunnelPingSampler(uuid4(), netns="sg-x", gateway=None)
        monkeypatch.setattr(sampler, "_targets", lambda: [("internet", "8.8.8.8")])
        with patch(
            "app.services.tunnel_ping_sampler.subprocess.Popen",
            side_effect=OSError("no ping"),
        ):
            rows = sampler._sample_window(datetime.now(UTC))
        assert rows == []

    def test_sample_window_parses_valid_output(self, monkeypatch) -> None:
        sampler = TunnelPingSampler(uuid4(), netns="sg-x", gateway=None)
        monkeypatch.setattr(sampler, "_targets", lambda: [("internet", "8.8.8.8")])
        output = (
            "55 packets transmitted, 55 received, 0% packet loss, time 54000ms\n"
            "rtt min/avg/max/mdev = 1.0/2.0/3.0/0.5 ms\n"
        )

        class FakeProc:
            def poll(self):
                return 0

            def communicate(self, timeout=None):
                return (output, None)

        with patch("app.services.tunnel_ping_sampler.subprocess.Popen", return_value=FakeProc()):
            rows = sampler._sample_window(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        assert len(rows) == 1
        assert rows[0].samples_sent == 55
        assert rows[0].avg_ms == 2.0
        assert rows[0].target == "internet"
