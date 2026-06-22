from datetime import UTC, date, datetime

from app.models.enums import CheckOutcome
from app.services.uptime_stats import (
    availability_percent,
    compute_downtime_seconds,
    status_from_outcomes,
)


class TestUptimeAggregation:
    def test_single_failure_stays_operational(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 99 + [CheckOutcome.TIMEOUT.value]
        assert status_from_outcomes(outcomes) == "operational"
        assert availability_percent(outcomes) == 99.0

    def test_many_failures_mark_outage(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 80 + [CheckOutcome.TIMEOUT.value] * 20
        assert status_from_outcomes(outcomes) == "outage"
        assert availability_percent(outcomes) == 80.0

    def test_moderate_failures_mark_degraded(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 95 + [CheckOutcome.TIMEOUT.value] * 5
        assert status_from_outcomes(outcomes) == "degraded"
        assert availability_percent(outcomes) == 95.0

    def test_degraded_counts_as_available(self) -> None:
        outcomes = [CheckOutcome.DEGRADED.value]
        assert status_from_outcomes(outcomes) == "operational"


class TestDowntimeCalculation:
    def test_no_events_is_zero(self) -> None:
        day = date(2026, 6, 20)
        assert compute_downtime_seconds([], day=day) == 0

    def test_outage_between_checks(self) -> None:
        day = date(2026, 6, 20)
        events = [
            (datetime(2026, 6, 20, 10, 0, tzinfo=UTC), CheckOutcome.UP.value),
            (datetime(2026, 6, 20, 10, 5, tzinfo=UTC), CheckOutcome.DOWN.value),
            (datetime(2026, 6, 20, 10, 35, tzinfo=UTC), CheckOutcome.UP.value),
        ]
        assert compute_downtime_seconds(events, day=day) == 30 * 60

    def test_still_down_at_end_of_day(self) -> None:
        day = date(2026, 6, 20)
        events = [
            (datetime(2026, 6, 20, 10, 0, tzinfo=UTC), CheckOutcome.DOWN.value),
        ]
        now = datetime(2026, 6, 20, 15, 0, tzinfo=UTC)
        assert compute_downtime_seconds(events, day=day, now=now) == 5 * 3600

    def test_degraded_does_not_count_as_downtime(self) -> None:
        day = date(2026, 6, 20)
        events = [
            (datetime(2026, 6, 20, 10, 0, tzinfo=UTC), CheckOutcome.DEGRADED.value),
            (datetime(2026, 6, 20, 11, 0, tzinfo=UTC), CheckOutcome.UP.value),
        ]
        assert compute_downtime_seconds(events, day=day) == 0

    def test_multiple_outage_periods(self) -> None:
        day = date(2026, 6, 20)
        events = [
            (datetime(2026, 6, 20, 9, 0, tzinfo=UTC), CheckOutcome.DOWN.value),
            (datetime(2026, 6, 20, 9, 10, tzinfo=UTC), CheckOutcome.UP.value),
            (datetime(2026, 6, 20, 12, 0, tzinfo=UTC), CheckOutcome.TIMEOUT.value),
            (datetime(2026, 6, 20, 12, 30, tzinfo=UTC), CheckOutcome.UP.value),
        ]
        assert compute_downtime_seconds(events, day=day) == 40 * 60

    def test_continuing_outage_from_previous_day(self) -> None:
        day = date(2026, 6, 21)
        events = [
            (datetime(2026, 6, 21, 1, 0, tzinfo=UTC), CheckOutcome.UP.value),
        ]
        assert compute_downtime_seconds(events, day=day, continuing_outage=True) == 3600

    def test_full_day_downtime_when_outage_continues_without_checks(self) -> None:
        day = date(2026, 6, 21)
        now = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
        assert compute_downtime_seconds([], day=day, now=now, continuing_outage=True) == 12 * 3600
