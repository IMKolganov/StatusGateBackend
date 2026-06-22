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
from app.services.uptime_stats import (
    DayCheckStats,
    availability_percent,
    compute_downtime_seconds,
    day_check_counts,
    empty_day_stats,
    is_outage_outcome,
    status_from_outcomes,
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
            checks_by_component_day = self._day_stats_by_component_day(
                component_ids, range_start, range_end, include_downtime=False
            )
            project_outcomes = _collect_outcomes(component_ids, day_keys, checks_by_component_day)
            summaries.append(
                PublicProjectSummary(
                    id=project.id,
                    name=project.name,
                    slug=project.slug,
                    description=project.description,
                    uptime_percent=_uptime_percent_from_outcomes(project_outcomes),
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

        checks_by_component_day = self._day_stats_by_component_day(component_ids, range_start, range_end)
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
                service_outcomes: list[str] = []

                for day in day_keys:
                    stats = checks_by_component_day.get((component.id, day), empty_day_stats())
                    service_outcomes.extend(stats.outcomes)
                    day_status = status_from_outcomes(stats.outcomes)
                    service_days.append(
                        _build_day_bar(
                            day=day,
                            day_status=day_status,
                            outcomes=stats.outcomes,
                            downtime_seconds=stats.downtime_seconds,
                            incidents=incidents_by_day.get(day, []),
                        )
                    )

                service_timelines.append(
                    PublicServiceTimeline(
                        id=component.id,
                        name=component.name,
                        slug=component.slug,
                        component_kind=kind_name,
                        uptime_percent=_uptime_percent_from_outcomes(service_outcomes),
                        days=service_days,
                    )
                )

            group_outcomes = _collect_outcomes(
                [component.id for component in kind_components],
                day_keys,
                checks_by_component_day,
            )
            group_days = [
                _build_day_bar(
                    day=day,
                    day_status=group_day_statuses[day],
                    outcomes=_collect_outcomes(
                        [component.id for component in kind_components],
                        [day],
                        checks_by_component_day,
                    ),
                    downtime_seconds=_max_downtime_for_day(
                        [component.id for component in kind_components],
                        day,
                        checks_by_component_day,
                    ),
                    incidents=incidents_by_day.get(day, []),
                )
                for day in day_keys
            ]
            groups.append(
                PublicComponentGroupTimeline(
                    name=kind_name,
                    component_count=len(kind_components),
                    uptime_percent=_uptime_percent_from_outcomes(group_outcomes),
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

    def _day_stats_by_component_day(
        self,
        component_ids: list[UUID],
        range_start: date,
        range_end: date,
        *,
        include_downtime: bool = True,
    ) -> dict[tuple[UUID, date], DayCheckStats]:
        if not component_ids:
            return {}

        start_dt = datetime.combine(range_start, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(range_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        now = datetime.now(UTC)

        rows = self._session.execute(
            select(
                CheckResult.monitored_component_id,
                CheckResult.checked_at,
                CheckResult.outcome,
            )
            .where(
                CheckResult.monitored_component_id.in_(component_ids),
                CheckResult.checked_at >= start_dt,
                CheckResult.checked_at < end_dt,
            )
            .order_by(CheckResult.checked_at.asc())
        ).all()

        pre_range_outcomes = (
            self._last_outcomes_before(component_ids, start_dt) if include_downtime else {}
        )

        events_by_key: dict[tuple[UUID, date], list[tuple[datetime, str]]] = defaultdict(list)
        for component_id, checked_at, outcome in rows:
            day = checked_at.astimezone(UTC).date()
            events_by_key[(component_id, day)].append((checked_at, outcome))

        stats: dict[tuple[UUID, date], DayCheckStats] = {}
        for key, events in events_by_key.items():
            component_id, day = key
            downtime_seconds = 0
            if include_downtime:
                continuing_outage = is_outage_outcome(
                    _outcome_at_end_of_previous_day(
                        component_id,
                        day,
                        range_start,
                        pre_range_outcomes,
                        events_by_key,
                    )
                )
                downtime_seconds = compute_downtime_seconds(
                    events,
                    day=day,
                    now=now,
                    continuing_outage=continuing_outage,
                )
            stats[key] = DayCheckStats(
                outcomes=[outcome for _, outcome in events],
                downtime_seconds=downtime_seconds,
            )
        return stats

    def _last_outcomes_before(
        self,
        component_ids: list[UUID],
        before: datetime,
    ) -> dict[UUID, str]:
        if not component_ids:
            return {}

        rows = self._session.execute(
            select(CheckResult.monitored_component_id, CheckResult.outcome)
            .where(
                CheckResult.monitored_component_id.in_(component_ids),
                CheckResult.checked_at < before,
            )
            .order_by(CheckResult.monitored_component_id, CheckResult.checked_at.desc())
            .distinct(CheckResult.monitored_component_id)
        ).all()
        return {component_id: outcome for component_id, outcome in rows}

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


def _collect_outcomes(
    component_ids: list[UUID],
    day_keys: list[date],
    day_stats_by_component_day: dict[tuple[UUID, date], DayCheckStats],
) -> list[str]:
    outcomes: list[str] = []
    for component_id in component_ids:
        for day in day_keys:
            stats = day_stats_by_component_day.get((component_id, day))
            if stats:
                outcomes.extend(stats.outcomes)
    return outcomes


def _outcome_at_end_of_previous_day(
    component_id: UUID,
    day: date,
    range_start: date,
    pre_range_outcomes: dict[UUID, str],
    events_by_key: dict[tuple[UUID, date], list[tuple[datetime, str]]],
) -> str | None:
    prev_day = day - timedelta(days=1)
    prev_events = events_by_key.get((component_id, prev_day))
    if prev_events:
        return prev_events[-1][1]
    if day == range_start:
        return pre_range_outcomes.get(component_id)
    return None


def _max_downtime_for_day(
    component_ids: list[UUID],
    day: date,
    day_stats_by_component_day: dict[tuple[UUID, date], DayCheckStats],
) -> int:
    if not component_ids:
        return 0
    return max(
        day_stats_by_component_day.get((component_id, day), empty_day_stats()).downtime_seconds
        for component_id in component_ids
    )


def _project_day_statuses(
    component_ids: list[UUID],
    day_keys: list[date],
    day_stats_by_component_day: dict[tuple[UUID, date], DayCheckStats],
) -> dict[date, str]:
    project_day_statuses: dict[date, str] = {day: "no_data" for day in day_keys}
    for component_id in component_ids:
        for day in day_keys:
            stats = day_stats_by_component_day.get((component_id, day), empty_day_stats())
            day_status = status_from_outcomes(stats.outcomes)
            project_day_statuses[day] = _merge_status(project_day_statuses[day], day_status)
    return project_day_statuses


def _merge_status(current: str, incoming: str) -> str:
    if STATUS_PRIORITY[incoming] > STATUS_PRIORITY[current]:
        return incoming
    return current


def _uptime_percent_from_outcomes(outcomes: list[str]) -> float | None:
    return availability_percent(outcomes)


def _build_day_bar(
    *,
    day: date,
    day_status: str,
    outcomes: list[str],
    downtime_seconds: int = 0,
    incidents: list[PublicDayIncident],
) -> PublicDayBar:
    total, up, degraded, failed = day_check_counts(outcomes)
    availability = availability_percent(outcomes)

    tooltip_parts: list[str] = []
    if total:
        summary = f"{total} checks: {up} ok"
        if degraded:
            summary += f", {degraded} degraded"
        if failed:
            summary += f", {failed} failed"
        tooltip_parts.append(summary)
        if availability is not None:
            tooltip_parts.append(f"{availability:.2f}% availability")
    else:
        tooltip_parts.append("No checks")

    if incidents:
        tooltip_parts.append(f"{len(incidents)} incident{'s' if len(incidents) != 1 else ''}")

    return PublicDayBar(
        date=day,
        status=day_status,
        tooltip=" · ".join(tooltip_parts),
        check_count=total,
        failed_count=failed,
        degraded_count=degraded,
        availability_percent=availability,
        downtime_seconds=downtime_seconds,
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
