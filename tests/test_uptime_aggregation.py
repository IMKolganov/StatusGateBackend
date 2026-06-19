from app.models.enums import CheckOutcome
from app.services.public_status_service import (
    _availability_percent,
    _status_from_outcomes,
)


class TestUptimeAggregation:
    def test_single_failure_stays_operational(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 99 + [CheckOutcome.TIMEOUT.value]
        assert _status_from_outcomes(outcomes) == "operational"
        assert _availability_percent(outcomes) == 99.0

    def test_many_failures_mark_outage(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 80 + [CheckOutcome.TIMEOUT.value] * 20
        assert _status_from_outcomes(outcomes) == "outage"
        assert _availability_percent(outcomes) == 80.0

    def test_moderate_failures_mark_degraded(self) -> None:
        outcomes = [CheckOutcome.UP.value] * 95 + [CheckOutcome.TIMEOUT.value] * 5
        assert _status_from_outcomes(outcomes) == "degraded"
        assert _availability_percent(outcomes) == 95.0

    def test_degraded_counts_as_available(self) -> None:
        outcomes = [CheckOutcome.DEGRADED.value]
        assert _status_from_outcomes(outcomes) == "operational"
