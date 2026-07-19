from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.engine.cursor import CursorResult
from sqlalchemy.orm import Session

from app.models.check_result import CheckResult
from app.models.connection_event import ConnectionEvent
from app.models.enums import VPN_CHECK_TYPES, ConnectionMode, PERSISTENT_VPN_CHECK_TYPES
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings
from app.models.project import Project
from app.services.health_check_service import run_health_check
from app.services.speed_test_config import (
    SpeedTestRunContext,
    effective_speed_test_url_template,
    extract_last_successful_speed_test,
    extract_speed_test_from_details,
    pick_staggered_speed_test_component_ids,
    should_run_speed_test,
)


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

    def count_for_component(self, component_id: UUID) -> int:
        stmt = select(func.count()).select_from(CheckResult).where(
            CheckResult.monitored_component_id == component_id
        )
        return self._session.scalar(stmt) or 0

    def purge_for_component(self, component_id: UUID, *, keep: int = 0) -> tuple[int, int]:
        if keep < 0:
            raise ValueError("keep must be non-negative")

        total = self.count_for_component(component_id)
        if total == 0 or keep >= total:
            return 0, total

        keep_ids = (
            select(CheckResult.id)
            .where(CheckResult.monitored_component_id == component_id)
            .order_by(CheckResult.checked_at.desc())
            .limit(keep)
        )
        delete_stmt = (
            delete(CheckResult)
            .where(CheckResult.monitored_component_id == component_id)
            .where(CheckResult.id.not_in(keep_ids))
        )
        result = self._session.execute(delete_stmt)
        deleted = cast(CursorResult[Any], result).rowcount or 0
        remaining = total - deleted
        return deleted, remaining


class ConnectionEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_component_paginated(
        self,
        component_id: UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ConnectionEvent], int]:
        filters = [ConnectionEvent.monitored_component_id == component_id]
        count_stmt = select(func.count()).select_from(ConnectionEvent).where(*filters)
        total = self._session.scalar(count_stmt) or 0
        stmt = (
            select(ConnectionEvent)
            .where(*filters)
            .order_by(ConnectionEvent.occurred_at.desc(), ConnectionEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all()), total

    def purge_for_component(self, component_id: UUID) -> int:
        delete_stmt = delete(ConnectionEvent).where(ConnectionEvent.monitored_component_id == component_id)
        result = self._session.execute(delete_stmt)
        return cast(CursorResult[Any], result).rowcount or 0


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
        if (
            component.check_type in PERSISTENT_VPN_CHECK_TYPES
            and component.connection_mode == ConnectionMode.PERSISTENT.value
        ):
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

    def run_check(
        self,
        component: MonitoredComponent,
        *,
        speed_test_allowed_ids: set[UUID] | None = None,
    ) -> CheckResult:
        settings = self.get_settings()
        speed_test_context: SpeedTestRunContext | None = None
        if component.check_type in VPN_CHECK_TYPES:
            latest_map = self._results_repo.latest_by_component_ids([component.id])
            latest = latest_map.get(component.id)
            latest_details = latest.details if latest and isinstance(latest.details, dict) else None
            checked_at = latest.checked_at if latest else None
            previous_speed_test = extract_speed_test_from_details(latest_details, checked_at=checked_at)
            last_successful_speed_test = extract_last_successful_speed_test(
                latest_details,
                checked_at=checked_at,
            )
            due = should_run_speed_test(component, settings, latest)
            if speed_test_allowed_ids is None:
                run_speed = due
            else:
                run_speed = due and component.id in speed_test_allowed_ids
            speed_test_context = SpeedTestRunContext(
                url_template=effective_speed_test_url_template(component, settings),
                run_speed_test=run_speed,
                previous_speed_test=previous_speed_test,
                last_successful_speed_test=last_successful_speed_test,
            )
        result = run_health_check(component, speed_test_context=speed_test_context)
        component.last_checked_at = result.checked_at
        self._session.add(component)
        return self._results_repo.add(result)

    def run_due_checks(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        due = self.list_due_components()
        due.sort(key=lambda component: (component.check_type not in VPN_CHECK_TYPES, str(component.id)))
        settings = self.get_settings()
        vpn_due = [component for component in due if component.check_type in VPN_CHECK_TYPES]
        latest_map = self._results_repo.latest_by_component_ids([component.id for component in vpn_due])
        allowed_speed_ids = pick_staggered_speed_test_component_ids(vpn_due, settings, latest_map)
        for component in due:
            results.append(self.run_check(component, speed_test_allowed_ids=allowed_speed_ids))
        self._session.commit()
        return results
