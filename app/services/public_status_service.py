from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.cqrs.common import PaginationParams
from app.cqrs.queries.projects import ProjectQueryHandler
from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome, IncidentUpdateStatus
from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.models.monitored_component import MonitoredComponent
from app.schemas.public_status import (
    PublicActiveAlert,
    PublicComponentGroupTimeline,
    PublicDayBar,
    PublicDayIncident,
    PublicProjectStatus,
    PublicProjectSummary,
    PublicServiceStatus,
    PublicServiceTimeline,
    PublicSystemStatus,
)
from app.services.vpn_check_service import public_network_summary

OUTAGE_OUTCOMES = {
    CheckOutcome.DOWN.value,
    CheckOutcome.TIMEOUT.value,
    CheckOutcome.ERROR.value,
}
DEGRADED_OUTCOMES = {CheckOutcome.DEGRADED.value}
ACTIVE_INCIDENT_STATUSES = {
    IncidentUpdateStatus.INVESTIGATING.value,
    IncidentUpdateStatus.IDENTIFIED.value,
    IncidentUpdateStatus.MONITORING.value,
}
STATUS_PRIORITY = {"outage": 3, "degraded": 2, "operational": 1, "no_data": 0}


class PublicStatusService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._project_queries = ProjectQueryHandler(session)

    def list_projects(self, *, limit: int = 100, days: int = 90) -> list[PublicProjectSummary]:
        result = self._project_queries.list_active_paginated(PaginationParams(offset=0, limit=limit))
        range_end = datetime.now(UTC).date()
        range_start = range_end - timedelta(days=days - 1)
        day_keys = _date_range(range_start, range_end)

        summaries: list[PublicProjectSummary] = []
        for project in result.items:
            components = self._load_components(project.id)
            component_ids = [component.id for component in components]
            checks_by_component_day = self._checks_by_component_day(component_ids, range_start, range_end)
            project_day_statuses = _project_day_statuses(component_ids, day_keys, checks_by_component_day)
            summaries.append(
                PublicProjectSummary(
                    id=project.id,
                    name=project.name,
                    slug=project.slug,
                    description=project.description,
                    uptime_percent=_uptime_percent(list(project_day_statuses.values())),
                )
            )
        return summaries

    def get_project_status(self, slug: str) -> PublicProjectStatus:
        project = self._project_queries.get_by_slug(slug)
        if project is None or not project.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

        components = self._load_components(project.id)
        latest_by_component = self._latest_check_results([component.id for component in components])

        services = []
        for component in components:
            latest = latest_by_component.get(component.id)
            services.append(
                PublicServiceStatus(
                    id=component.id,
                    name=component.name,
                    slug=component.slug,
                    description=component.description,
                    environment=component.environment,
                    component_kind=component.component_kind.name,
                    status=latest[0] if latest else "unknown",
                    latency_ms=latest[1] if latest else None,
                    checked_at=latest[2] if latest else None,
                    network_summary=latest[3] if latest else None,
                )
            )

        return PublicProjectStatus(
            id=project.id,
            name=project.name,
            slug=project.slug,
            description=project.description,
            services=services,
        )

    def get_system_status(self, slug: str, *, end: date | None = None, days: int = 90) -> PublicSystemStatus:
        project = self._project_queries.get_by_slug(slug)
        if project is None or not project.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

        range_end = end or datetime.now(UTC).date()
        range_start = range_end - timedelta(days=days - 1)
        day_keys = _date_range(range_start, range_end)

        components = self._load_components(project.id)
        component_ids = [component.id for component in components]

        checks_by_component_day = self._checks_by_component_day(component_ids, range_start, range_end)
        incidents_by_day = self._incidents_by_day(project.id, range_start, range_end)

        groups_map: dict[str, list[MonitoredComponent]] = defaultdict(list)
        for component in components:
            groups_map[component.component_kind.name].append(component)

        groups: list[PublicComponentGroupTimeline] = []
        latest_by_component = self._latest_check_results(component_ids)
        for kind_name in sorted(groups_map):
            kind_components = sorted(groups_map[kind_name], key=lambda item: item.name.lower())
            service_timelines: list[PublicServiceTimeline] = []
            group_day_statuses = {
                day: status
                for day, status in _project_day_statuses(
                    [component.id for component in kind_components],
                    day_keys,
                    checks_by_component_day,
                ).items()
            }

            for component in kind_components:
                service_days: list[PublicDayBar] = []
                service_statuses: list[str] = []

                for day in day_keys:
                    outcomes = checks_by_component_day.get((component.id, day), [])
                    day_status = _status_from_outcomes(outcomes)
                    service_statuses.append(day_status)
                    service_days.append(
                        _build_day_bar(
                            day=day,
                            day_status=day_status,
                            incidents=incidents_by_day.get(day, []),
                        )
                    )

                service_timelines.append(
                    PublicServiceTimeline(
                        id=component.id,
                        name=component.name,
                        slug=component.slug,
                        component_kind=kind_name,
                        uptime_percent=_uptime_percent(service_statuses),
                        days=service_days,
                    )
                )

            group_days = [
                _build_day_bar(
                    day=day,
                    day_status=group_day_statuses[day],
                    incidents=incidents_by_day.get(day, []),
                )
                for day in day_keys
            ]
            groups.append(
                PublicComponentGroupTimeline(
                    name=kind_name,
                    component_count=len(kind_components),
                    uptime_percent=_uptime_percent(list(group_day_statuses.values())),
                    days=group_days,
                    services=service_timelines,
                )
            )

        return PublicSystemStatus(
            project_id=project.id,
            project_name=project.name,
            project_slug=project.slug,
            range_start=range_start,
            range_end=range_end,
            range_label=_format_range_label(range_start, range_end),
            days=days,
            groups=groups,
            active_alerts=self._active_alerts(project.id, components, latest_by_component),
        )

    def _load_components(self, project_id: UUID) -> list[MonitoredComponent]:
        return list(
            self._session.scalars(
                select(MonitoredComponent)
                .where(
                    MonitoredComponent.project_id == project_id,
                    MonitoredComponent.is_active.is_(True),
                )
                .options(selectinload(MonitoredComponent.component_kind))
                .order_by(MonitoredComponent.name.asc())
            ).all()
        )

    def _latest_check_results(
        self,
        component_ids: list[UUID],
    ) -> dict[UUID, tuple[str, int | None, datetime | None, dict | None]]:
        if not component_ids:
            return {}

        stmt = (
            select(CheckResult)
            .where(CheckResult.monitored_component_id.in_(component_ids))
            .order_by(CheckResult.monitored_component_id, CheckResult.checked_at.desc())
            .distinct(CheckResult.monitored_component_id)
        )
        rows = self._session.scalars(stmt).all()
        return {
            row.monitored_component_id: (
                row.outcome,
                row.latency_ms,
                row.checked_at,
                public_network_summary(row.details if isinstance(row.details, dict) else None),
            )
            for row in rows
        }

    def _checks_by_component_day(
        self,
        component_ids: list[UUID],
        range_start: date,
        range_end: date,
    ) -> dict[tuple[UUID, date], list[str]]:
        if not component_ids:
            return {}

        start_dt = datetime.combine(range_start, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(range_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

        rows = self._session.scalars(
            select(CheckResult)
            .where(
                CheckResult.monitored_component_id.in_(component_ids),
                CheckResult.checked_at >= start_dt,
                CheckResult.checked_at < end_dt,
            )
            .order_by(CheckResult.checked_at.asc())
        ).all()

        grouped: dict[tuple[UUID, date], list[str]] = defaultdict(list)
        for row in rows:
            day = row.checked_at.astimezone(UTC).date()
            grouped[(row.monitored_component_id, day)].append(row.outcome)
        return grouped

    def _incidents_by_day(
        self,
        project_id: UUID,
        range_start: date,
        range_end: date,
    ) -> dict[date, list[PublicDayIncident]]:
        start_dt = datetime.combine(range_start, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(range_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

        rows = self._session.scalars(
            select(IncidentUpdate)
            .join(Incident, Incident.id == IncidentUpdate.incident_id)
            .where(
                Incident.project_id == project_id,
                IncidentUpdate.posted_at >= start_dt,
                IncidentUpdate.posted_at < end_dt,
            )
            .options(selectinload(IncidentUpdate.incident))
            .order_by(IncidentUpdate.posted_at.asc())
        ).all()

        grouped: dict[date, list[PublicDayIncident]] = defaultdict(list)
        for row in rows:
            day = row.posted_at.astimezone(UTC).date()
            grouped[day].append(
                PublicDayIncident(
                    title=row.incident.title,
                    message=row.message,
                    status=row.status,
                    posted_at=row.posted_at,
                )
            )
        return grouped

    def _active_alerts(
        self,
        project_id: UUID,
        components: list[MonitoredComponent],
        latest_by_component: dict[UUID, tuple[str, int | None, datetime | None]],
    ) -> list[PublicActiveAlert]:
        alerts: list[PublicActiveAlert] = []
        seen_titles: set[str] = set()

        for component in components:
            latest = latest_by_component.get(component.id)
            if latest is None:
                continue
            outcome = latest[0]
            if outcome in OUTAGE_OUTCOMES or outcome in DEGRADED_OUTCOMES:
                title = f"{component.name} — {_status_label(outcome)}"
                if title not in seen_titles:
                    seen_titles.add(title)
                    alerts.append(
                        PublicActiveAlert(
                            title=title,
                            message=f"Latest check reported {outcome}.",
                            status=outcome,
                            since=latest[2],
                        )
                    )

        incidents = self._session.scalars(
            select(Incident)
            .where(Incident.project_id == project_id)
            .options(selectinload(Incident.updates))
            .order_by(Incident.created_at.desc())
        ).all()

        for incident in incidents:
            if not incident.updates:
                continue
            latest_update = incident.updates[0]
            if latest_update.status not in ACTIVE_INCIDENT_STATUSES:
                continue
            if incident.title in seen_titles:
                continue
            seen_titles.add(incident.title)
            alerts.append(
                PublicActiveAlert(
                    title=incident.title,
                    message=latest_update.message,
                    status=latest_update.status,
                    since=latest_update.posted_at,
                )
            )

        return alerts


def _date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _status_from_outcomes(outcomes: list[str]) -> str:
    if not outcomes:
        return "no_data"
    if any(outcome in OUTAGE_OUTCOMES for outcome in outcomes):
        return "outage"
    if any(outcome in DEGRADED_OUTCOMES for outcome in outcomes):
        return "degraded"
    return "operational"


def _project_day_statuses(
    component_ids: list[UUID],
    day_keys: list[date],
    checks_by_component_day: dict[tuple[UUID, date], list[str]],
) -> dict[date, str]:
    project_day_statuses: dict[date, str] = {day: "no_data" for day in day_keys}
    for component_id in component_ids:
        for day in day_keys:
            outcomes = checks_by_component_day.get((component_id, day), [])
            day_status = _status_from_outcomes(outcomes)
            project_day_statuses[day] = _merge_status(project_day_statuses[day], day_status)
    return project_day_statuses


def _merge_status(current: str, incoming: str) -> str:
    if STATUS_PRIORITY[incoming] > STATUS_PRIORITY[current]:
        return incoming
    return current


def _uptime_percent(statuses: list[str]) -> float | None:
    counted = [status for status in statuses if status != "no_data"]
    if not counted:
        return None
    operational = sum(1 for status in counted if status == "operational")
    return round(operational / len(counted) * 100, 2)


def _build_day_bar(
    *,
    day: date,
    day_status: str,
    incidents: list[PublicDayIncident],
) -> PublicDayBar:
    if incidents:
        tooltip = f"{len(incidents)} incident{'s' if len(incidents) != 1 else ''}"
    else:
        tooltip = "No incidents"
    return PublicDayBar(
        date=day,
        status=day_status,
        tooltip=tooltip,
        incidents=incidents,
    )


def _format_range_label(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return start.strftime("%b %Y")
    if start.year == end.year:
        return f"{start.strftime('%b')}–{end.strftime('%b %Y')}"
    return f"{start.strftime('%b %Y')}–{end.strftime('%b %Y')}"


def _status_label(outcome: str) -> str:
    labels = {
        CheckOutcome.UP.value: "Operational",
        CheckOutcome.DOWN.value: "Outage",
        CheckOutcome.DEGRADED.value: "Degraded",
        CheckOutcome.TIMEOUT.value: "Timeout",
        CheckOutcome.ERROR.value: "Error",
    }
    return labels.get(outcome, outcome.title())
