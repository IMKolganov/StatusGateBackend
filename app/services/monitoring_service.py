from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings
from app.models.project import Project
from app.services.health_check_service import run_health_check


class MonitoringSettingsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> MonitoringSettings:
        row = self._session.get(MonitoringSettings, MONITORING_SETTINGS_ID)
        if row is None:
            row = MonitoringSettings(id=MONITORING_SETTINGS_ID)
            self._session.add(row)
            self._session.flush()
        return row

    def save(self, settings: MonitoringSettings) -> MonitoringSettings:
        self._session.add(settings)
        self._session.flush()
        return settings


class CheckResultRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, result: CheckResult) -> CheckResult:
        self._session.add(result)
        self._session.flush()
        return result

    def list_for_component_paginated(
        self,
        component_id: UUID,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[CheckResult], int]:
        filters = [CheckResult.monitored_component_id == component_id]
        count_stmt = select(func.count()).select_from(CheckResult).where(*filters)
        total = self._session.scalar(count_stmt) or 0
        stmt = (
            select(CheckResult)
            .where(*filters)
            .order_by(CheckResult.checked_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all()), total

    def latest_by_component_ids(self, component_ids: list[UUID]) -> dict[UUID, CheckResult]:
        if not component_ids:
            return {}
        subq = (
            select(
                CheckResult.monitored_component_id,
                func.max(CheckResult.checked_at).label("max_checked_at"),
            )
            .where(CheckResult.monitored_component_id.in_(component_ids))
            .group_by(CheckResult.monitored_component_id)
            .subquery()
        )
        stmt = select(CheckResult).join(
            subq,
            (CheckResult.monitored_component_id == subq.c.monitored_component_id)
            & (CheckResult.checked_at == subq.c.max_checked_at),
        )
        results = self._session.scalars(stmt).all()
        return {result.monitored_component_id: result for result in results}


class HealthCheckRunner:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._settings_repo = MonitoringSettingsRepository(session)
        self._results_repo = CheckResultRepository(session)

    def get_settings(self) -> MonitoringSettings:
        return self._settings_repo.get()

    def effective_poll_interval(self, component: MonitoredComponent, settings: MonitoringSettings) -> int:
        return component.poll_interval_seconds or settings.default_poll_interval_seconds

    def is_due(self, component: MonitoredComponent, settings: MonitoringSettings, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if not component.is_active:
            return False
        interval = self.effective_poll_interval(component, settings)
        if component.last_checked_at is None:
            return True
        last_checked = component.last_checked_at
        if last_checked.tzinfo is None:
            last_checked = last_checked.replace(tzinfo=UTC)
        return current - last_checked >= timedelta(seconds=interval)

    def list_due_components(self, *, now: datetime | None = None) -> list[MonitoredComponent]:
        settings = self.get_settings()
        current = now or datetime.now(UTC)
        stmt = (
            select(MonitoredComponent)
            .join(Project, MonitoredComponent.project_id == Project.id)
            .where(MonitoredComponent.is_active.is_(True), Project.is_active.is_(True))
        )
        components = list(self._session.scalars(stmt).all())
        return [component for component in components if self.is_due(component, settings, now=current)]

    def run_check(self, component: MonitoredComponent) -> CheckResult:
        result = run_health_check(component)
        component.last_checked_at = result.checked_at
        self._session.add(component)
        return self._results_repo.add(result)

    def run_due_checks(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for component in self.list_due_components():
            results.append(self.run_check(component))
        self._session.commit()
        return results
