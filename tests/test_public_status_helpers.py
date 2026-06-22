from datetime import UTC, date, datetime
from uuid import uuid4

from app.models.enums import CheckOutcome
from app.services.public_status_service import (
    _build_day_bar,
    _collect_outcomes,
    _max_downtime_for_day,
    _merge_status,
    _outcome_at_end_of_previous_day,
)
from app.services.uptime_stats import (
    DayCheckStats,
    availability_percent,
    day_check_counts,
    empty_day_stats,
    is_outage_outcome,
    status_from_outcomes,
)


class TestUptimeStatsHelpers:
    def test_empty_day_stats(self) -> None:
        stats = empty_day_stats()
        assert stats.outcomes == []
        assert stats.downtime_seconds == 0

    def test_is_outage_outcome(self) -> None:
        assert is_outage_outcome(CheckOutcome.DOWN.value)
        assert is_outage_outcome(CheckOutcome.TIMEOUT.value)
        assert is_outage_outcome(CheckOutcome.ERROR.value)
        assert not is_outage_outcome(CheckOutcome.UP.value)
        assert not is_outage_outcome(CheckOutcome.DEGRADED.value)

    def test_day_check_counts(self) -> None:
        outcomes = [
            CheckOutcome.UP.value,
            CheckOutcome.UP.value,
            CheckOutcome.DEGRADED.value,
            CheckOutcome.DOWN.value,
        ]
        assert day_check_counts(outcomes) == (4, 2, 1, 1)

    def test_availability_percent_empty(self) -> None:
        assert availability_percent([]) is None

    def test_status_from_outcomes_no_data(self) -> None:
        assert status_from_outcomes([]) == "no_data"


class TestPublicStatusHelpers:
    def test_outcome_at_end_of_previous_day_from_in_range_events(self) -> None:
        component_id = uuid4()
        range_start = date(2026, 6, 21)
        events_by_key = {
            (component_id, date(2026, 6, 20)): [
                (datetime(2026, 6, 20, 23, 0, tzinfo=UTC), CheckOutcome.DOWN.value),
            ],
        }

        outcome = _outcome_at_end_of_previous_day(
            component_id,
            date(2026, 6, 21),
            range_start,
            {},
            events_by_key,
        )
        assert outcome == CheckOutcome.DOWN.value

    def test_outcome_at_end_of_previous_day_from_pre_range(self) -> None:
        component_id = uuid4()
        range_start = date(2026, 6, 21)
        pre_range = {component_id: CheckOutcome.TIMEOUT.value}

        outcome = _outcome_at_end_of_previous_day(
            component_id,
            range_start,
            range_start,
            pre_range,
            {},
        )
        assert outcome == CheckOutcome.TIMEOUT.value

    def test_outcome_at_end_of_previous_day_missing(self) -> None:
        component_id = uuid4()
        range_start = date(2026, 6, 21)

        outcome = _outcome_at_end_of_previous_day(
            component_id,
            date(2026, 6, 22),
            range_start,
            {},
            {},
        )
        assert outcome is None

    def test_collect_outcomes_skips_missing_days(self) -> None:
        component_id = uuid4()
        day = date(2026, 6, 20)
        stats = {
            (component_id, day): DayCheckStats(
                outcomes=[CheckOutcome.UP.value, CheckOutcome.DEGRADED.value],
                downtime_seconds=0,
            ),
        }

        outcomes = _collect_outcomes([component_id], [day, date(2026, 6, 21)], stats)
        assert outcomes == [CheckOutcome.UP.value, CheckOutcome.DEGRADED.value]

    def test_max_downtime_for_day(self) -> None:
        first = uuid4()
        second = uuid4()
        day = date(2026, 6, 20)
        stats = {
            (first, day): DayCheckStats(outcomes=[CheckOutcome.UP.value], downtime_seconds=600),
            (second, day): DayCheckStats(outcomes=[CheckOutcome.UP.value], downtime_seconds=1800),
        }

        assert _max_downtime_for_day([first, second], day, stats) == 1800
        assert _max_downtime_for_day([], day, stats) == 0

    def test_merge_status_prefers_worse_state(self) -> None:
        assert _merge_status("operational", "degraded") == "degraded"
        assert _merge_status("degraded", "outage") == "outage"
        assert _merge_status("outage", "operational") == "outage"

    def test_build_day_bar_includes_downtime_and_counts(self) -> None:
        day = date(2026, 6, 20)
        outcomes = [CheckOutcome.UP.value, CheckOutcome.DEGRADED.value]

        bar = _build_day_bar(
            day=day,
            day_status="operational",
            outcomes=outcomes,
            downtime_seconds=900,
            incidents=[],
        )

        assert bar.downtime_seconds == 900
        assert bar.check_count == 2
        assert bar.degraded_count == 1
        assert bar.failed_count == 0
        assert bar.availability_percent == 100.0
        assert "2 checks: 1 ok, 1 degraded" in bar.tooltip

    def test_build_day_bar_no_checks(self) -> None:
        bar = _build_day_bar(
            day=date(2026, 6, 20),
            day_status="no_data",
            outcomes=[],
            incidents=[],
        )

        assert bar.downtime_seconds == 0
        assert bar.check_count == 0
        assert bar.tooltip == "No checks"
