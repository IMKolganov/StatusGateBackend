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
    outcomes: list[str]
    downtime_seconds: int


def empty_day_stats() -> DayCheckStats:
    return DayCheckStats(outcomes=[], downtime_seconds=0)


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


def availability_percent(outcomes: list[str]) -> float | None:
    if not outcomes:
        return None
    total, up, degraded, _failed = day_check_counts(outcomes)
    return round((up + degraded) / total * 100, 2)


DAY_OPERATIONAL_MIN_AVAILABILITY = 99.0
DAY_DEGRADED_MIN_AVAILABILITY = 90.0


def status_from_outcomes(outcomes: list[str]) -> str:
    if not outcomes:
        return "no_data"

    availability = availability_percent(outcomes)
    assert availability is not None
    if availability >= DAY_OPERATIONAL_MIN_AVAILABILITY:
        return "operational"
    if availability >= DAY_DEGRADED_MIN_AVAILABILITY:
        return "degraded"
    return "outage"
