from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.check_result import CheckResult
from app.models.enums import CheckType
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings
from app.services.speed_test_config import (
    DEFAULT_SPEED_TEST_URL_TEMPLATE,
    SpeedTestRunContext,
    build_speed_test_url,
    is_rate_limited_speed_test,
    reset_cloudflare_speed_test_slot_for_tests,
    should_run_speed_test,
    speed_test_rate_warning,
    try_acquire_cloudflare_speed_test_slot,
    validate_speed_test_url_template,
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


def _vpn_component(**overrides) -> MonitoredComponent:
    base = {
        "id": uuid4(),
        "project_id": uuid4(),
        "component_kind_id": uuid4(),
        "name": "VPN",
        "slug": "vpn",
        "check_url": "https://ifconfig.me/ip",
        "check_method": "GET",
        "check_type": CheckType.OPENVPN.value,
        "check_config": {"config_text": "client\ndev tun\nremote vpn.example.com 1194\n"},
        "expected_status_code": 200,
        "timeout_seconds": 30,
        "speed_test_enabled": True,
        "is_active": True,
    }
    base.update(overrides)
    return MonitoredComponent(**base)


class TestSpeedTestConfig:
    def test_validate_speed_test_url_template_requires_bytes_placeholder(self) -> None:
        with pytest.raises(ValueError):
            validate_speed_test_url_template("https://example.com/download")

    def test_build_speed_test_url(self) -> None:
        url = build_speed_test_url("https://example.com/file?size={bytes}", 1024)
        assert url == "https://example.com/file?size=1024"

    def test_should_run_speed_test_respects_interval(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        settings = _settings(default_speed_test_interval_seconds=3600)
        measured_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=30),
            outcome="up",
            details={"network": {"speed_test": {"ok": True, "bytes": 1024, "measured_at": measured_at}}},
        )
        assert should_run_speed_test(component, settings, latest) is False

    def test_should_run_speed_test_uses_measured_at_not_checked_at(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        settings = _settings(default_speed_test_interval_seconds=3600)
        measured_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=15),
            outcome="up",
            details={"network": {"speed_test": {"ok": True, "bytes": 1024, "measured_at": measured_at}}},
        )
        assert should_run_speed_test(component, settings, latest) is True

    def test_pick_staggered_speed_test_component_ids_limits_to_one(self) -> None:
        from app.services.speed_test_config import pick_staggered_speed_test_component_ids

        settings = _settings(default_speed_test_interval_seconds=3600)
        components = [_vpn_component(slug=f"vpn-{i}") for i in range(3)]
        latest_by_id = {}
        for component in components:
            latest_by_id[component.id] = CheckResult(
                monitored_component_id=component.id,
                checked_at=datetime.now(UTC) - timedelta(hours=2),
                outcome="up",
                details={
                    "network": {
                        "speed_test": {
                            "ok": True,
                            "measured_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                        }
                    }
                },
            )
        allowed = pick_staggered_speed_test_component_ids(components, settings, latest_by_id, limit=1)
        assert len(allowed) == 1
        assert next(iter(allowed)) in {component.id for component in components}

    def test_pick_staggered_speed_test_rotates_order_by_hour(self) -> None:
        from app.services.speed_test_config import (
            pick_staggered_speed_test_component_ids,
            speed_test_stagger_key,
        )

        settings = _settings(default_speed_test_interval_seconds=3600)
        components = [_vpn_component(slug=f"vpn-{i}") for i in range(4)]
        hour_a = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
        hour_b = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
        measured_at = (hour_a - timedelta(hours=3)).isoformat()
        latest_by_id = {
            component.id: CheckResult(
                monitored_component_id=component.id,
                checked_at=hour_a - timedelta(seconds=10),
                outcome="up",
                details={"network": {"speed_test": {"ok": True, "measured_at": measured_at}}},
            )
            for component in components
        }
        ids = [component.id for component in components]
        # Hourly salt must change each component's sort key (order of the whole set may still collide).
        assert any(
            speed_test_stagger_key(component_id, now=hour_a)
            != speed_test_stagger_key(component_id, now=hour_b)
            for component_id in ids
        )

        first_a = next(
            iter(pick_staggered_speed_test_component_ids(components, settings, latest_by_id, now=hour_a, limit=1))
        )
        first_b = next(
            iter(pick_staggered_speed_test_component_ids(components, settings, latest_by_id, now=hour_b, limit=1))
        )
        order_a = sorted(ids, key=lambda component_id: speed_test_stagger_key(component_id, now=hour_a))
        order_b = sorted(ids, key=lambda component_id: speed_test_stagger_key(component_id, now=hour_b))
        assert first_a == order_a[0]
        assert first_b == order_b[0]

    def test_is_meaningful_speed_test_success_rejects_zero_download(self) -> None:
        from app.services.speed_test_config import is_meaningful_speed_test_success

        assert is_meaningful_speed_test_success({"ok": True, "bytes": 10485760, "mbps": 91.2}) is True
        assert is_meaningful_speed_test_success({"ok": True, "bytes": 0, "mbps": 0.0}) is False
        assert is_meaningful_speed_test_success({"ok": True, "bytes": 100, "mbps": 0.0}) is False
        assert is_meaningful_speed_test_success({"ok": False, "bytes": 0}) is False

    def test_pick_display_rejects_zero_mbps_success(self) -> None:
        from app.services.speed_test_config import pick_display_speed_test

        displayed = pick_display_speed_test(
            {"ok": True, "bytes": 0, "mbps": 0.0, "cached": True},
            {"ok": True, "bytes": 10485760, "mbps": 114.6, "measured_at": "2026-07-20T00:00:00+00:00"},
        )
        assert displayed is not None
        assert displayed["mbps"] == 114.6
        assert displayed.get("stale") is True

        failed = pick_display_speed_test({"ok": True, "bytes": 0, "mbps": 0.0}, None)
        assert failed is not None
        assert failed["ok"] is False
        assert "no data" in str(failed.get("error", "")).lower()

    def test_update_speed_test_stats_tracks_min_max_avg(self) -> None:
        from app.services.speed_test_config import update_speed_test_stats

        first = update_speed_test_stats(None, mbps=100.0)
        assert first == {"min_mbps": 100.0, "max_mbps": 100.0, "avg_mbps": 100.0, "sample_count": 1}
        second = update_speed_test_stats(first, mbps=50.0)
        assert second["min_mbps"] == 50.0
        assert second["max_mbps"] == 100.0
        assert second["avg_mbps"] == 75.0
        assert second["sample_count"] == 2
        third = update_speed_test_stats(second, mbps=150.0)
        assert third["min_mbps"] == 50.0
        assert third["max_mbps"] == 150.0
        assert third["avg_mbps"] == 100.0
        assert third["sample_count"] == 3

    def test_update_speed_test_stats_handles_varied_sequences(self) -> None:
        from app.services.speed_test_config import update_speed_test_stats

        values = [12.5, 91.88, 0.01, 200.0, 45.3, 45.3]
        stats = None
        for value in values:
            stats = update_speed_test_stats(stats, mbps=value)
        assert stats is not None
        assert stats["min_mbps"] == 0.01
        assert stats["max_mbps"] == 200.0
        assert stats["sample_count"] == 6
        assert stats["avg_mbps"] == round(sum(values) / len(values), 2)

    def test_extract_speed_test_stats_reads_and_seeds(self) -> None:
        from app.services.speed_test_config import extract_speed_test_stats

        explicit = extract_speed_test_stats(
            {
                "network": {
                    "speed_test_stats": {
                        "min_mbps": 40.0,
                        "max_mbps": 120.0,
                        "avg_mbps": 80.0,
                        "sample_count": 3,
                    }
                }
            }
        )
        assert explicit == {
            "min_mbps": 40.0,
            "max_mbps": 120.0,
            "avg_mbps": 80.0,
            "sample_count": 3,
        }

        seeded = extract_speed_test_stats(
            {
                "network": {
                    "speed_test_last_success": {
                        "ok": True,
                        "bytes": 10485760,
                        "mbps": 114.6,
                    }
                }
            }
        )
        assert seeded == {
            "min_mbps": 114.6,
            "max_mbps": 114.6,
            "avg_mbps": 114.6,
            "sample_count": 1,
        }
        assert extract_speed_test_stats(
            {
                "network": {
                    "speed_test_last_success": {"ok": True, "bytes": 1, "mbps": 0.0},
                }
            }
        ) is None
        assert extract_speed_test_stats(
            {
                "network": {
                    "speed_test_stats": {
                        "min_mbps": 10,
                        "max_mbps": 5,
                        "avg_mbps": 7,
                        "sample_count": 2,
                    }
                }
            }
        ) is None

    def test_resolve_speed_test_memory_recovers_after_empty_down(self) -> None:
        from app.services.speed_test_config import resolve_speed_test_memory

        history = {
            "network": {
                "speed_test": {
                    "ok": True,
                    "bytes": 10485760,
                    "mbps": 14.76,
                    "measured_at": "2026-08-03T11:19:01.577624+00:00",
                },
                "speed_test_last_success": {
                    "ok": True,
                    "bytes": 10485760,
                    "mbps": 14.76,
                    "measured_at": "2026-08-03T11:19:01.577624+00:00",
                },
                "speed_test_stats": {
                    "min_mbps": 7.91,
                    "max_mbps": 20.01,
                    "avg_mbps": 14.76,
                    "sample_count": 4,
                },
            }
        }
        # Latest is a tunnel-down row (or deferred placeholder with no carry-forward).
        latest_down = {"network": {"connect_time_ms": 511}}
        previous, last_success, stats = resolve_speed_test_memory(
            latest_down,
            latest_checked_at=datetime(2026, 8, 4, 20, 20, 44, tzinfo=UTC),
            history_details=history,
            history_checked_at=datetime(2026, 8, 4, 20, 19, 39, tzinfo=UTC),
        )
        assert previous is None
        assert last_success is not None
        assert last_success["mbps"] == 14.76
        assert stats == {
            "min_mbps": 7.91,
            "max_mbps": 20.01,
            "avg_mbps": 14.76,
            "sample_count": 4,
        }

        latest_deferred = {
            "network": {
                "speed_test": {
                    "ok": False,
                    "error": "Speed test deferred (waiting for a free slot among VPN services)",
                    "deferred": True,
                }
            }
        }
        previous, last_success, stats = resolve_speed_test_memory(
            latest_deferred,
            history_details=history,
            history_checked_at=datetime(2026, 8, 4, 20, 19, 39, tzinfo=UTC),
        )
        assert previous is not None
        assert previous.get("deferred") is True
        assert last_success is not None
        assert last_success["mbps"] == 14.76
        assert stats["sample_count"] == 4

    def test_should_run_speed_test_cached_previous_respects_interval(self) -> None:
        """Cached/deferred/throttled previous must NOT reset the interval clock."""
        component = _vpn_component(speed_test_interval_seconds=3600)
        settings = _settings(default_speed_test_interval_seconds=3600)
        measured_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        for flags in (
            {"cached": True, "deferred": True, "defer_reason": "stagger"},
            {"cached": True, "deferred": True, "throttled": True, "defer_reason": "slot"},
        ):
            latest = CheckResult(
                monitored_component_id=component.id,
                checked_at=datetime.now(UTC) - timedelta(seconds=15),
                outcome="up",
                details={
                    "network": {
                        "speed_test": {
                            "ok": True,
                            "mbps": 12.0,
                            "bytes": 1024,
                            "measured_at": measured_at,
                            **flags,
                        }
                    }
                },
            )
            assert should_run_speed_test(component, settings, latest) is False

    def test_should_run_speed_test_cached_previous_due_after_interval(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=300)
        settings = _settings(default_speed_test_interval_seconds=300)
        measured_at = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=15),
            outcome="up",
            details={
                "network": {
                    "speed_test": {
                        "ok": True,
                        "mbps": 80.0,
                        "bytes": 10485760,
                        "measured_at": measured_at,
                        "cached": True,
                        "deferred": True,
                        "defer_reason": "stagger",
                    }
                }
            },
        )
        assert should_run_speed_test(component, settings, latest) is True

    def test_should_run_speed_test_uses_last_attempt_when_display_is_cached(self) -> None:
        """After a live 429, a later cached success row must still honor 429 backoff."""
        component = _vpn_component(speed_test_interval_seconds=60)
        settings = _settings(default_speed_test_interval_seconds=60)
        rate_limited_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        old_success_at = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=15),
            outcome="up",
            details={
                "network": {
                    "speed_test": {
                        "ok": True,
                        "mbps": 90.0,
                        "bytes": 10485760,
                        "measured_at": old_success_at,
                        "cached": True,
                        "deferred": True,
                        "defer_reason": "stagger",
                    },
                    "speed_test_last_attempt": {
                        "ok": False,
                        "error": "Speed test rate limited (HTTP 429)",
                        "measured_at": rate_limited_at,
                    },
                    "speed_test_last_success": {
                        "ok": True,
                        "mbps": 90.0,
                        "bytes": 10485760,
                        "measured_at": old_success_at,
                    },
                }
            },
        )
        # 429 backoff is 3600s even when interval is 60s.
        assert should_run_speed_test(component, settings, latest) is False

    def test_should_run_speed_test_cached_without_measured_at_stays_due(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=3600)
        settings = _settings(default_speed_test_interval_seconds=3600)
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=15),
            outcome="up",
            details={
                "network": {
                    "speed_test": {
                        "ok": True,
                        "mbps": 12.0,
                        "cached": True,
                        "deferred": True,
                    }
                }
            },
        )
        assert should_run_speed_test(component, settings, latest) is True

    def test_pick_staggered_skips_components_still_inside_cached_interval(self) -> None:
        from app.services.speed_test_config import pick_staggered_speed_test_component_ids

        settings = _settings(default_speed_test_interval_seconds=3600)
        recent = _vpn_component(slug="recent")
        stale = _vpn_component(slug="stale")
        now = datetime.now(UTC)
        latest_by_id = {
            recent.id: CheckResult(
                monitored_component_id=recent.id,
                checked_at=now - timedelta(seconds=30),
                outcome="up",
                details={
                    "network": {
                        "speed_test": {
                            "ok": True,
                            "mbps": 50.0,
                            "bytes": 1024,
                            "measured_at": (now - timedelta(minutes=10)).isoformat(),
                            "cached": True,
                            "deferred": True,
                            "defer_reason": "stagger",
                        }
                    }
                },
            ),
            stale.id: CheckResult(
                monitored_component_id=stale.id,
                checked_at=now - timedelta(seconds=30),
                outcome="up",
                details={
                    "network": {
                        "speed_test": {
                            "ok": True,
                            "mbps": 40.0,
                            "bytes": 1024,
                            "measured_at": (now - timedelta(hours=2)).isoformat(),
                            "cached": True,
                            "deferred": True,
                            "defer_reason": "stagger",
                        }
                    }
                },
            ),
        }
        allowed = pick_staggered_speed_test_component_ids(
            [recent, stale], settings, latest_by_id, now=now, limit=1
        )
        assert allowed == {stale.id}

    def test_scheduler_cycles_respect_interval_across_cached_rows(self) -> None:
        """Simulate poll cycles: after a live success, cached deferrals stay not-due until interval."""
        from app.services.speed_test_config import pick_staggered_speed_test_component_ids

        component = _vpn_component(slug="persistent-vpn", speed_test_interval_seconds=300)
        settings = _settings(default_speed_test_interval_seconds=300)
        t0 = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        live_at = t0.isoformat()

        # Cycle 0: live success
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=t0,
            outcome="up",
            details={
                "network": {
                    "speed_test": {
                        "ok": True,
                        "mbps": 72.0,
                        "bytes": 10485760,
                        "measured_at": live_at,
                    }
                }
            },
        )
        assert should_run_speed_test(component, settings, latest, now=t0 + timedelta(seconds=60)) is False

        # Cycles 1..4: deferred/cached rows carrying the same measured_at (+ last_attempt)
        for minutes in (1, 2, 3, 4):
            now = t0 + timedelta(minutes=minutes)
            latest = CheckResult(
                monitored_component_id=component.id,
                checked_at=now,
                outcome="up",
                details={
                    "network": {
                        "speed_test": {
                            "ok": True,
                            "mbps": 72.0,
                            "bytes": 10485760,
                            "measured_at": live_at,
                            "cached": True,
                            "deferred": True,
                            "defer_reason": "stagger",
                        },
                        "speed_test_last_attempt": {
                            "ok": True,
                            "mbps": 72.0,
                            "bytes": 10485760,
                            "measured_at": live_at,
                        },
                    }
                },
            )
            assert should_run_speed_test(component, settings, latest, now=now) is False
            allowed = pick_staggered_speed_test_component_ids(
                [component], settings, {component.id: latest}, now=now, limit=1
            )
            assert allowed == set()

        # After 5 minutes the component becomes due again
        now = t0 + timedelta(minutes=5, seconds=1)
        assert should_run_speed_test(component, settings, latest, now=now) is True
        allowed = pick_staggered_speed_test_component_ids(
            [component], settings, {component.id: latest}, now=now, limit=1
        )
        assert allowed == {component.id}

    def test_hydrate_speed_test_measured_at_for_legacy_live_rows(self) -> None:
        from app.services.speed_test_config import extract_speed_test_from_details

        checked_at = datetime(2026, 7, 20, 0, 10, tzinfo=UTC)
        details = {"network": {"speed_test": {"ok": True, "mbps": 91.88, "bytes": 10485760}}}
        hydrated = extract_speed_test_from_details(details, checked_at=checked_at)
        assert hydrated is not None
        assert hydrated["measured_at"] == checked_at.isoformat()

        cached = extract_speed_test_from_details(
            {"network": {"speed_test": {"ok": True, "mbps": 10.0, "cached": True}}},
            checked_at=checked_at,
        )
        assert cached is not None
        assert "measured_at" not in cached

    def test_speed_test_rate_warning_for_many_services(self) -> None:
        settings = _settings(default_poll_interval_seconds=60, default_speed_test_interval_seconds=60)
        components = [_vpn_component(slug=f"vpn-{index}", speed_test_interval_seconds=60) for index in range(12)]
        warning = speed_test_rate_warning(components, settings)
        assert warning is not None
        assert "speed.cloudflare.com" in warning
        assert "HTTP requests" in warning
        assert str(len(components)) in warning

    def test_speed_test_context_default(self) -> None:
        context = SpeedTestRunContext.default()
        assert context.run_speed_test is True
        assert "{bytes}" in context.url_template

    def test_should_not_retry_immediately_after_rate_limit(self) -> None:
        component = _vpn_component(speed_test_interval_seconds=60)
        settings = _settings(default_speed_test_interval_seconds=60)
        measured_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        latest = CheckResult(
            monitored_component_id=component.id,
            checked_at=datetime.now(UTC) - timedelta(seconds=30),
            outcome="up",
            details={
                "network": {
                    "speed_test": {
                        "ok": False,
                        "error": "Speed test rate limited (HTTP 429)",
                        "measured_at": measured_at,
                    },
                }
            },
        )
        # 429 backoff is 3600s even when interval is 60s.
        assert should_run_speed_test(component, settings, latest) is False

    def test_cloudflare_speed_test_slot_limits_burst(self) -> None:
        reset_cloudflare_speed_test_slot_for_tests()
        assert try_acquire_cloudflare_speed_test_slot(now=100.0) is True
        assert try_acquire_cloudflare_speed_test_slot(now=120.0) is False
        assert try_acquire_cloudflare_speed_test_slot(now=161.0) is True

    def test_is_rate_limited_speed_test(self) -> None:
        assert is_rate_limited_speed_test({"ok": False, "error": "Speed test rate limited (HTTP 429)"}) is True
        assert is_rate_limited_speed_test({"ok": True, "mbps": 10.0}) is False
