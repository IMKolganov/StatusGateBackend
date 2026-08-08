from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.component_kind import ComponentKind
from app.models.enums import CheckOutcome
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.services.monitoring_service import CheckResultRepository, fetch_latest_check_results
from app.services.public_status_service import PublicStatusService
from app.services.uptime_stats import (
    DayCheckStats,
    availability_from_stats,
    status_from_stats,
)


def _seed_component(db_session: Session, *, slug: str = "api") -> MonitoredComponent:
    kind = ComponentKind(name="APIs", slug=f"kind-{slug}-{uuid4().hex[:8]}")
    project = Project(name="Query Demo", slug=f"proj-{slug}-{uuid4().hex[:8]}", is_active=True)
    db_session.add_all([kind, project])
    db_session.flush()
    component = MonitoredComponent(
        project_id=project.id,
        component_kind_id=kind.id,
        name="API",
        slug=slug,
        check_url="https://example.com/health",
        check_method="GET",
        expected_status_code=200,
        timeout_seconds=10,
        is_active=True,
    )
    db_session.add(component)
    db_session.flush()
    return component


class TestFetchLatestCheckResults:
    def test_returns_latest_row_per_component(self, db_session: Session) -> None:
        first = _seed_component(db_session, slug="first")
        second = _seed_component(db_session, slug="second")
        older = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        newer = datetime(2026, 8, 1, 11, 0, tzinfo=UTC)

        db_session.add_all(
            [
                CheckResult(
                    monitored_component_id=first.id,
                    checked_at=older,
                    outcome=CheckOutcome.DOWN.value,
                    latency_ms=10,
                ),
                CheckResult(
                    monitored_component_id=first.id,
                    checked_at=newer,
                    outcome=CheckOutcome.UP.value,
                    latency_ms=20,
                ),
                CheckResult(
                    monitored_component_id=second.id,
                    checked_at=older,
                    outcome=CheckOutcome.DEGRADED.value,
                    latency_ms=30,
                ),
            ]
        )
        db_session.commit()

        rows = fetch_latest_check_results(db_session, [first.id, second.id])
        by_id = {row.monitored_component_id: row for row in rows}

        assert by_id[first.id].outcome == CheckOutcome.UP.value
        assert by_id[first.id].checked_at == newer
        assert by_id[second.id].outcome == CheckOutcome.DEGRADED.value

    def test_before_filter_ignores_newer_rows(self, db_session: Session) -> None:
        component = _seed_component(db_session, slug="before")
        older = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        newer = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        db_session.add_all(
            [
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=older,
                    outcome=CheckOutcome.TIMEOUT.value,
                ),
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=newer,
                    outcome=CheckOutcome.UP.value,
                ),
            ]
        )
        db_session.commit()

        rows = fetch_latest_check_results(
            db_session,
            [component.id],
            before=datetime(2026, 8, 1, 11, 0, tzinfo=UTC),
        )
        assert len(rows) == 1
        assert rows[0].outcome == CheckOutcome.TIMEOUT.value

    def test_repository_uses_lateral_helper(self, db_session: Session) -> None:
        component = _seed_component(db_session, slug="repo")
        db_session.add(
            CheckResult(
                monitored_component_id=component.id,
                checked_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
                outcome=CheckOutcome.UP.value,
            )
        )
        db_session.commit()

        latest = CheckResultRepository(db_session).latest_by_component_ids([component.id])
        assert component.id in latest
        assert latest[component.id].outcome == CheckOutcome.UP.value


class TestDayStatsAggregation:
    def test_outcome_counts_without_downtime(self, db_session: Session) -> None:
        component = _seed_component(db_session, slug="counts")
        day = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
        db_session.add_all(
            [
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day,
                    outcome=CheckOutcome.UP.value,
                ),
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day + timedelta(minutes=1),
                    outcome=CheckOutcome.UP.value,
                ),
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day + timedelta(minutes=2),
                    outcome=CheckOutcome.DOWN.value,
                ),
            ]
        )
        db_session.commit()

        service = PublicStatusService(db_session)
        stats = service._day_stats_by_component_day(
            [component.id],
            date(2026, 8, 2),
            date(2026, 8, 2),
            include_downtime=False,
        )
        day_stats = stats[(component.id, date(2026, 8, 2))]
        assert day_stats.total == 3
        assert day_stats.up == 2
        assert day_stats.failed == 1
        assert day_stats.downtime_seconds == 0
        assert availability_from_stats(day_stats) == 66.67
        assert status_from_stats(day_stats) == "outage"

    def test_downtime_path_merges_counts_and_seconds(self, db_session: Session) -> None:
        component = _seed_component(db_session, slug="downtime")
        day = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        db_session.add_all(
            [
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day,
                    outcome=CheckOutcome.UP.value,
                ),
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day + timedelta(minutes=5),
                    outcome=CheckOutcome.DOWN.value,
                ),
                CheckResult(
                    monitored_component_id=component.id,
                    checked_at=day + timedelta(minutes=20),
                    outcome=CheckOutcome.UP.value,
                ),
            ]
        )
        db_session.commit()

        service = PublicStatusService(db_session)
        stats = service._day_stats_by_component_day(
            [component.id],
            date(2026, 8, 3),
            date(2026, 8, 3),
            include_downtime=True,
        )
        day_stats = stats[(component.id, date(2026, 8, 3))]
        assert day_stats.total == 3
        assert day_stats.up == 2
        assert day_stats.failed == 1
        assert day_stats.downtime_seconds == 15 * 60


class TestDayCheckStatsHelpers:
    def test_from_outcomes_and_with_downtime(self) -> None:
        stats = DayCheckStats.from_outcomes(
            [CheckOutcome.UP.value, CheckOutcome.DEGRADED.value, CheckOutcome.DOWN.value],
            downtime_seconds=0,
        )
        assert stats.total == 3
        assert stats.up == 1
        assert stats.degraded == 1
        assert stats.failed == 1
        with_down = stats.with_downtime(120)
        assert with_down.downtime_seconds == 120
        assert with_down.total == 3
