from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.models.enums import CheckOutcome

OUTAGE_OUTCOMES = {
    CheckOutcome.DOWN.value,
    CheckOutcome.TIMEOUT.value,
    CheckOutcome.ERROR.value,
}
DEGRADED_OUTCOMES = {CheckOutcome.DEGRADED.value}


@dataclass(frozen=True)
class DayCheckStats:
    downtime_seconds: int = 0
    total: int = 0
    up: int = 0
    degraded: int = 0
    failed: int = 0

    @classmethod
    def from_outcomes(cls, outcomes: list[str], *, downtime_seconds: int = 0) -> "DayCheckStats":
        total, up, degraded, failed = day_check_counts(outcomes)
        return cls(
            downtime_seconds=downtime_seconds,
            total=total,
            up=up,
            degraded=degraded,
            failed=failed,
        )

    def with_downtime(self, downtime_seconds: int) -> "DayCheckStats":
        return DayCheckStats(
            downtime_seconds=downtime_seconds,
            total=self.total,
            up=self.up,
            degraded=self.degraded,
            failed=self.failed,
        )


def empty_day_stats() -> DayCheckStats:
    return DayCheckStats()


def is_outage_outcome(outcome: str) -> bool:
    return outcome in OUTAGE_OUTCOMES


def compute_downtime_seconds(
    events: list[tuple[datetime, str]],
    *,
    day: date,
    now: datetime | None = None,
    continuing_outage: bool = False,
) -> int:
    current = now or datetime.now(UTC)
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    period_end = min(day_end, current)

    total = 0
    down_since: datetime | None = day_start if continuing_outage else None

    if not events:
        if down_since is not None:
            return max(int((period_end - down_since).total_seconds()), 0)
        return 0

    for checked_at, outcome in sorted(events, key=lambda item: item[0]):
        if is_outage_outcome(outcome):
            if down_since is None:
                down_since = checked_at
            continue

        if down_since is not None:
            total += max(int((checked_at - down_since).total_seconds()), 0)
            down_since = None

    if down_since is not None:
        total += max(int((period_end - down_since).total_seconds()), 0)

    return total


def day_check_counts(outcomes: list[str]) -> tuple[int, int, int, int]:
    total = len(outcomes)
    up = sum(1 for outcome in outcomes if outcome == CheckOutcome.UP.value)
    degraded = sum(1 for outcome in outcomes if outcome in DEGRADED_OUTCOMES)
    failed = sum(1 for outcome in outcomes if outcome in OUTAGE_OUTCOMES)
    return total, up, degraded, failed


def availability_from_counts(total: int, up: int, degraded: int) -> float | None:
    if total <= 0:
        return None
    return round((up + degraded) / total * 100, 2)


def availability_percent(outcomes: list[str]) -> float | None:
    if not outcomes:
        return None
    total, up, degraded, _failed = day_check_counts(outcomes)
    return availability_from_counts(total, up, degraded)


def availability_from_stats(stats: DayCheckStats) -> float | None:
    return availability_from_counts(stats.total, stats.up, stats.degraded)


DAY_OPERATIONAL_MIN_AVAILABILITY = 99.0
DAY_DEGRADED_MIN_AVAILABILITY = 90.0


def status_from_availability(availability: float | None) -> str:
    if availability is None:
        return "no_data"
    if availability >= DAY_OPERATIONAL_MIN_AVAILABILITY:
        return "operational"
    if availability >= DAY_DEGRADED_MIN_AVAILABILITY:
        return "degraded"
    return "outage"


def status_from_outcomes(outcomes: list[str]) -> str:
    return status_from_availability(availability_percent(outcomes))


def status_from_stats(stats: DayCheckStats) -> str:
    return status_from_availability(availability_from_stats(stats))
