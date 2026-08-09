from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session, selectinload

from app.cqrs.common import PaginationParams
from app.cqrs.queries.projects import ProjectQueryHandler
from app.models.check_result import CheckResult
from app.models.connection_event import ConnectionEvent
from app.models.enums import CheckOutcome, IncidentUpdateStatus
from app.models.incident import Incident
from app.models.monitored_component import MonitoredComponent
from app.models.tunnel_ping_sample import TunnelPingSample
from app.schemas.network import NetworkSummary
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
    PublicTunnelConnectionEvent,
    PublicTunnelLatestDiagnostics,
    PublicTunnelMetricPoint,
    PublicTunnelMetrics,
    PublicTunnelPingSample,
)
from app.services.monitoring_service import fetch_latest_check_results
from app.services.uptime_stats import (
    DayCheckStats,
    availability_from_stats,
    compute_downtime_seconds,
    empty_day_stats,
    is_outage_outcome,
    status_from_stats,
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
            project_stats = _sum_stats(component_ids, day_keys, checks_by_component_day)
            summaries.append(
                PublicProjectSummary(
                    id=project.id,
                    name=project.name,
                    slug=project.slug,
                    description=project.description,
                    uptime_percent=availability_from_stats(project_stats),
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

    def get_tunnel_metrics(
        self,
        project_slug: str,
        service_slug: str,
        *,
        hours: int = 2,
    ) -> PublicTunnelMetrics:
        hours = max(2, min(int(hours), 24))
        project = self._project_queries.get_by_slug(project_slug)
        if project is None or not project.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

        component = self._session.scalar(
            select(MonitoredComponent)
            .where(
                MonitoredComponent.project_id == project.id,
                MonitoredComponent.slug == service_slug,
                MonitoredComponent.is_active.is_(True),
            )
            .options(selectinload(MonitoredComponent.component_kind))
        )
        if component is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

        range_end = datetime.now(UTC)
        range_start = range_end - timedelta(hours=hours)

        check_rows = self._session.scalars(
            select(CheckResult)
            .where(
                CheckResult.monitored_component_id == component.id,
                CheckResult.checked_at >= range_start,
                CheckResult.checked_at <= range_end,
            )
            .order_by(CheckResult.checked_at.asc())
        ).all()

        points: list[PublicTunnelMetricPoint] = []
        for row in check_rows:
            points.append(
                _build_tunnel_metric_point(
                    checked_at=row.checked_at,
                    outcome=row.outcome,
                    latency_ms=row.latency_ms,
                    details=row.details if isinstance(row.details, dict) else None,
                )
            )

        sample_rows = self._session.scalars(
            select(TunnelPingSample)
            .where(
                TunnelPingSample.monitored_component_id == component.id,
                TunnelPingSample.bucket_start >= range_start,
                TunnelPingSample.bucket_start <= range_end,
            )
            .order_by(TunnelPingSample.bucket_start.asc(), TunnelPingSample.target.asc())
        ).all()
        ping_samples = [
            PublicTunnelPingSample(
                bucket_start=sample.bucket_start,
                target=sample.target,
                target_host=sample.target_host,
                samples_sent=sample.samples_sent,
                samples_received=sample.samples_received,
                loss_percent=sample.loss_percent,
                min_ms=sample.min_ms,
                avg_ms=sample.avg_ms,
                max_ms=sample.max_ms,
                jitter_ms=sample.jitter_ms,
            )
            for sample in sample_rows
        ]

        event_rows = self._session.scalars(
            select(ConnectionEvent)
            .where(
                ConnectionEvent.monitored_component_id == component.id,
                ConnectionEvent.occurred_at >= range_start,
                ConnectionEvent.occurred_at <= range_end,
            )
            .order_by(ConnectionEvent.occurred_at.asc(), ConnectionEvent.id.asc())
        ).all()
        events = [
            PublicTunnelConnectionEvent(
                id=event.id,
                occurred_at=event.occurred_at,
                event_type=event.event_type,
                outcome=event.outcome,
                message=event.message,
            )
            for event in event_rows
        ]

        return PublicTunnelMetrics(
            project_slug=project.slug,
            service_id=component.id,
            service_name=component.name,
            service_slug=component.slug,
            component_kind=component.component_kind.name,
            range_start=range_start,
            range_end=range_end,
            hours=hours,
            latest=_build_tunnel_latest_diagnostics(check_rows, points),
            points=points,
            ping_samples=ping_samples,
            events=events,
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
        timeline_incidents = self._load_timeline_incidents(project.id, range_start, range_end)

        groups_map: dict[str, list[MonitoredComponent]] = defaultdict(list)
        for component in components:
            groups_map[component.component_kind.name].append(component)

        groups: list[PublicComponentGroupTimeline] = []
        latest_by_component = self._latest_check_results(component_ids)
        for kind_name in sorted(groups_map):
            kind_components = sorted(groups_map[kind_name], key=lambda item: item.name.lower())
            kind_component_ids = {component.id for component in kind_components}
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
                service_stats = empty_day_stats()

                for day in day_keys:
                    stats = checks_by_component_day.get((component.id, day), empty_day_stats())
                    service_stats = _add_stats(service_stats, stats)
                    day_status = status_from_stats(stats)
                    service_days.append(
                        _build_day_bar(
                            day=day,
                            day_status=day_status,
                            stats=stats,
                            incidents=_day_incidents_for(
                                timeline_incidents,
                                day,
                                component_id=component.id,
                            ),
                        )
                    )

                service_timelines.append(
                    PublicServiceTimeline(
                        id=component.id,
                        name=component.name,
                        slug=component.slug,
                        component_kind=kind_name,
                        uptime_percent=availability_from_stats(service_stats),
                        days=service_days,
                    )
                )

            group_stats = _sum_stats(
                [component.id for component in kind_components],
                day_keys,
                checks_by_component_day,
            )
            group_days = [
                _build_day_bar(
                    day=day,
                    day_status=group_day_statuses[day],
                    stats=_sum_stats(
                        [component.id for component in kind_components],
                        [day],
                        checks_by_component_day,
                    ).with_downtime(
                        _max_downtime_for_day(
                            [component.id for component in kind_components],
                            day,
                            checks_by_component_day,
                        )
                    ),
                    incidents=_day_incidents_for(
                        timeline_incidents,
                        day,
                        component_ids=kind_component_ids,
                    ),
                )
                for day in day_keys
            ]
            groups.append(
                PublicComponentGroupTimeline(
                    name=kind_name,
                    component_count=len(kind_components),
                    uptime_percent=availability_from_stats(group_stats),
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
    ) -> dict[UUID, tuple[str, int | None, datetime | None, NetworkSummary | None]]:
        rows = fetch_latest_check_results(self._session, component_ids)
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
        stats = self._day_outcome_counts_by_component_day(component_ids, start_dt, end_dt)
        if not include_downtime:
            return stats

        downtimes = self._day_downtime_by_component_day(
            component_ids,
            range_start=range_start,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        for key, downtime_seconds in downtimes.items():
            current = stats.get(key, empty_day_stats())
            stats[key] = current.with_downtime(downtime_seconds)
        return stats

    def _day_downtime_by_component_day(
        self,
        component_ids: list[UUID],
        *,
        range_start: date,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[tuple[UUID, date], int]:
        now = datetime.now(UTC)
        day_expr = func.date(func.timezone("UTC", CheckResult.checked_at))
        is_down = CheckResult.outcome.in_(tuple(OUTAGE_OUTCOMES))
        rows = self._session.execute(
            select(
                CheckResult.monitored_component_id,
                day_expr.label("day"),
                func.array_agg(aggregate_order_by(CheckResult.checked_at, CheckResult.checked_at)).label(
                    "checked_ats"
                ),
                func.array_agg(aggregate_order_by(is_down, CheckResult.checked_at)).label("is_downs"),
            )
            .where(
                CheckResult.monitored_component_id.in_(component_ids),
                CheckResult.checked_at >= start_dt,
                CheckResult.checked_at < end_dt,
            )
            .group_by(CheckResult.monitored_component_id, day_expr)
        ).all()

        pre_range_outcomes = self._last_outcomes_before(component_ids, start_dt)
        events_by_key: dict[tuple[UUID, date], list[tuple[datetime, str]]] = {}
        for component_id, day, checked_ats, is_downs in rows:
            day_value = day if isinstance(day, date) else date.fromisoformat(str(day))
            events_by_key[(component_id, day_value)] = [
                (
                    checked_at,
                    CheckOutcome.DOWN.value if down_flag else CheckOutcome.UP.value,
                )
                for checked_at, down_flag in zip(checked_ats, is_downs, strict=True)
            ]

        downtimes: dict[tuple[UUID, date], int] = {}
        for key, events in events_by_key.items():
            component_id, day = key
            previous_outcome = _outcome_at_end_of_previous_day(
                component_id,
                day,
                range_start,
                pre_range_outcomes,
                events_by_key,
            )
            continuing_outage = previous_outcome is not None and is_outage_outcome(previous_outcome)
            downtimes[key] = compute_downtime_seconds(
                events,
                day=day,
                now=now,
                continuing_outage=continuing_outage,
            )
        return downtimes

    def _day_outcome_counts_by_component_day(
        self,
        component_ids: list[UUID],
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[tuple[UUID, date], DayCheckStats]:
        """Compact day stats for list/uptime views that do not need downtime timelines."""
        day_expr = func.date(func.timezone("UTC", CheckResult.checked_at))
        rows = self._session.execute(
            select(
                CheckResult.monitored_component_id,
                day_expr.label("day"),
                CheckResult.outcome,
                func.count().label("cnt"),
            )
            .where(
                CheckResult.monitored_component_id.in_(component_ids),
                CheckResult.checked_at >= start_dt,
                CheckResult.checked_at < end_dt,
            )
            .group_by(CheckResult.monitored_component_id, day_expr, CheckResult.outcome)
        ).all()

        counts_by_key: dict[tuple[UUID, date], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for component_id, day, outcome, cnt in rows:
            day_value = day if isinstance(day, date) else date.fromisoformat(str(day))
            counts_by_key[(component_id, day_value)][str(outcome)] += int(cnt)

        stats: dict[tuple[UUID, date], DayCheckStats] = {}
        for key, counts in counts_by_key.items():
            up = counts.get(CheckOutcome.UP.value, 0)
            degraded = counts.get(CheckOutcome.DEGRADED.value, 0)
            failed = sum(counts.get(outcome, 0) for outcome in OUTAGE_OUTCOMES)
            stats[key] = DayCheckStats(
                downtime_seconds=0,
                total=sum(counts.values()),
                up=up,
                degraded=degraded,
                failed=failed,
            )
        return stats

    def _last_outcomes_before(
        self,
        component_ids: list[UUID],
        before: datetime,
    ) -> dict[UUID, str]:
        rows = fetch_latest_check_results(self._session, component_ids, before=before)
        return {row.monitored_component_id: row.outcome for row in rows}

    def _load_timeline_incidents(
        self,
        project_id: UUID,
        range_start: date,
        range_end: date,
    ) -> list[Incident]:
        """Incidents whose [starts_at, ends_at|now] overlaps the visible UTC day range."""
        start_dt = datetime.combine(range_start, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(range_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        return list(
            self._session.scalars(
                select(Incident)
                .where(
                    Incident.project_id == project_id,
                    Incident.starts_at < end_dt,
                    or_(Incident.ends_at.is_(None), Incident.ends_at >= start_dt),
                )
                .options(
                    selectinload(Incident.updates),
                    selectinload(Incident.monitored_component),
                )
                .order_by(Incident.starts_at.asc())
            ).all()
        )

    def _active_alerts(
        self,
        project_id: UUID,
        components: list[MonitoredComponent],
        latest_by_component: dict[UUID, tuple[str, int | None, datetime | None, NetworkSummary | None]],
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


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _build_tunnel_metric_point(
    *,
    checked_at: datetime,
    outcome: str,
    latency_ms: int | None,
    details: dict[str, Any] | None,
) -> PublicTunnelMetricPoint:
    network = details.get("network") if isinstance(details, dict) else None
    if not isinstance(network, dict):
        network = {}

    ping = network.get("gateway_ping") if isinstance(network.get("gateway_ping"), dict) else {}
    probe = network.get("probe") if isinstance(network.get("probe"), dict) else {}
    google_probe = network.get("google_probe") if isinstance(network.get("google_probe"), dict) else {}
    speed = network.get("speed_test") if isinstance(network.get("speed_test"), dict) else {}
    upload = network.get("speed_test_upload") if isinstance(network.get("speed_test_upload"), dict) else {}
    direct_download = (
        network.get("direct_speed_test") if isinstance(network.get("direct_speed_test"), dict) else {}
    )
    direct_upload = (
        network.get("direct_speed_test_upload")
        if isinstance(network.get("direct_speed_test_upload"), dict)
        else {}
    )

    def _extract_speed(payload: dict[str, Any]) -> tuple[
        float | None, int | None, int | None, bool | None, bool | None, str | None
    ]:
        mbps: float | None = None
        bytes_count: int | None = None
        duration_ms: int | None = None
        cached: bool | None = None
        ok: bool | None = None
        measured_at = payload.get("measured_at") if isinstance(payload.get("measured_at"), str) else None
        if not payload:
            return mbps, bytes_count, duration_ms, cached, ok, measured_at
        if payload.get("ok") is True:
            ok = True
            value = _as_float(payload.get("mbps"))
            if value is not None and value > 0:
                mbps = value
                bytes_count = _as_int(payload.get("bytes"))
                duration_ms = _as_int(payload.get("duration_ms"))
                cached = bool(payload.get("cached") or payload.get("deferred") or payload.get("stale"))
            else:
                ok = False
        elif payload.get("ok") is False:
            ok = False
        return mbps, bytes_count, duration_ms, cached, ok, measured_at

    download_mbps, download_bytes, download_duration_ms, download_cached, speed_test_ok, measured_at = (
        _extract_speed(speed)
    )
    upload_mbps, upload_bytes, upload_duration_ms, upload_cached, upload_ok, upload_measured_at = (
        _extract_speed(upload)
    )
    direct_dl_mbps, _, _, direct_dl_cached, _, _ = _extract_speed(direct_download)
    direct_ul_mbps, _, _, direct_ul_cached, _, _ = _extract_speed(direct_upload)
    direct_measured_at = (
        network.get("direct_speed_test_measured_at")
        if isinstance(network.get("direct_speed_test_measured_at"), str)
        else None
    )
    if direct_measured_at is None and isinstance(direct_download.get("measured_at"), str):
        direct_measured_at = direct_download.get("measured_at")

    exit_ip = probe.get("exit_ip")
    if exit_ip is not None:
        exit_ip = str(exit_ip)

    return PublicTunnelMetricPoint(
        checked_at=checked_at,
        outcome=outcome,
        latency_ms=latency_ms,
        connect_time_ms=_as_int(network.get("connect_time_ms")),
        exit_ip=exit_ip,
        probe_latency_ms=_as_float(probe.get("latency_ms")),
        google_probe_ok=google_probe.get("ok") if isinstance(google_probe.get("ok"), bool) else None,
        google_probe_latency_ms=_as_float(google_probe.get("latency_ms")),
        gateway_ping_avg_ms=_as_float(ping.get("avg_ms")),
        gateway_ping_jitter_ms=_as_float(ping.get("jitter_ms")),
        gateway_ping_loss_percent=_as_float(ping.get("loss_percent")),
        download_mbps=download_mbps,
        download_bytes=download_bytes,
        download_duration_ms=download_duration_ms,
        download_cached=download_cached,
        upload_mbps=upload_mbps,
        upload_bytes=upload_bytes,
        upload_duration_ms=upload_duration_ms,
        upload_cached=upload_cached,
        direct_download_mbps=direct_dl_mbps,
        direct_download_cached=direct_dl_cached,
        direct_upload_mbps=direct_ul_mbps,
        direct_upload_cached=direct_ul_cached,
        speed_test_ok=speed_test_ok,
        speed_test_measured_at=measured_at,
        upload_speed_test_ok=upload_ok,
        upload_speed_test_measured_at=upload_measured_at,
        direct_speed_test_measured_at=direct_measured_at,
    )


def _build_tunnel_latest_diagnostics(
    check_rows: list[Any],
    points: list[PublicTunnelMetricPoint],
) -> PublicTunnelLatestDiagnostics | None:
    if not check_rows:
        return None

    latest_row = check_rows[-1]
    summary = public_network_summary(
        latest_row.details if isinstance(latest_row.details, dict) else None
    )

    healthy = sum(1 for point in points if point.outcome in {"up", "degraded"})
    uptime_percent = round(100.0 * healthy / len(points), 2) if points else None
    fresh_speed_tests = sum(
        1
        for point in points
        if point.download_mbps is not None
        and point.download_mbps > 0
        and point.download_cached is not True
    )

    if summary is None:
        return PublicTunnelLatestDiagnostics(
            checked_at=latest_row.checked_at,
            outcome=latest_row.outcome,
            fresh_speed_tests_in_window=fresh_speed_tests,
            uptime_percent=uptime_percent,
        )

    return PublicTunnelLatestDiagnostics(
        checked_at=latest_row.checked_at,
        outcome=latest_row.outcome,
        exit_ip=summary.exit_ip,
        connect_time_ms=summary.connect_time_ms,
        probe_latency_ms=_as_float(summary.probe_latency_ms),
        google_probe_ok=summary.google_probe_ok,
        google_probe_latency_ms=_as_float(summary.google_probe_latency_ms),
        gateway_ping_avg_ms=summary.gateway_ping_avg_ms,
        gateway_ping_jitter_ms=summary.gateway_ping_jitter_ms,
        gateway_ping_loss_percent=summary.gateway_ping_loss_percent,
        download_mbps=summary.download_mbps,
        download_bytes=summary.download_bytes,
        download_duration_ms=summary.download_duration_ms,
        speed_test_ok=summary.speed_test_ok,
        speed_test_error=summary.speed_test_error,
        speed_test_measured_at=summary.speed_test_measured_at,
        speed_test_last_success_at=summary.speed_test_last_success_at,
        speed_test_showing_last_success=summary.speed_test_showing_last_success,
        speed_test_min_mbps=summary.speed_test_min_mbps,
        speed_test_max_mbps=summary.speed_test_max_mbps,
        speed_test_avg_mbps=summary.speed_test_avg_mbps,
        speed_test_sample_count=summary.speed_test_sample_count,
        upload_mbps=summary.upload_mbps,
        upload_bytes=summary.upload_bytes,
        upload_duration_ms=summary.upload_duration_ms,
        upload_speed_test_ok=summary.upload_speed_test_ok,
        upload_speed_test_error=summary.upload_speed_test_error,
        upload_speed_test_measured_at=summary.upload_speed_test_measured_at,
        upload_speed_test_last_success_at=summary.upload_speed_test_last_success_at,
        upload_speed_test_showing_last_success=summary.upload_speed_test_showing_last_success,
        upload_speed_test_min_mbps=summary.upload_speed_test_min_mbps,
        upload_speed_test_max_mbps=summary.upload_speed_test_max_mbps,
        upload_speed_test_avg_mbps=summary.upload_speed_test_avg_mbps,
        upload_speed_test_sample_count=summary.upload_speed_test_sample_count,
        direct_download_mbps=summary.direct_download_mbps,
        direct_download_bytes=summary.direct_download_bytes,
        direct_download_duration_ms=summary.direct_download_duration_ms,
        direct_download_measured_at=summary.direct_download_measured_at,
        direct_upload_mbps=summary.direct_upload_mbps,
        direct_upload_bytes=summary.direct_upload_bytes,
        direct_upload_duration_ms=summary.direct_upload_duration_ms,
        direct_upload_measured_at=summary.direct_upload_measured_at,
        direct_speed_test_skip_reason=summary.direct_speed_test_skip_reason,
        fresh_speed_tests_in_window=fresh_speed_tests,
        uptime_percent=uptime_percent,
    )


def _date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _incident_overlaps_day(incident: Incident, day: date, *, now: datetime) -> bool:
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    range_end = incident.ends_at or now
    starts_at = incident.starts_at
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=UTC)
    return starts_at < day_end and range_end >= day_start


def _incident_matches_scope(
    incident: Incident,
    *,
    component_id: UUID | None = None,
    component_ids: set[UUID] | None = None,
) -> bool:
    if incident.monitored_component_id is None:
        return True
    if component_id is not None:
        return incident.monitored_component_id == component_id
    if component_ids is not None:
        return incident.monitored_component_id in component_ids
    return True


def _public_day_incident(incident: Incident, day: date) -> PublicDayIncident:
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    updates = sorted(incident.updates, key=lambda item: item.posted_at)
    latest = None
    for update in updates:
        posted_at = update.posted_at if update.posted_at.tzinfo else update.posted_at.replace(tzinfo=UTC)
        if posted_at < day_end:
            latest = update
    if latest is None and updates:
        latest = updates[-1]
    component = incident.monitored_component
    return PublicDayIncident(
        title=incident.title,
        message=latest.message if latest is not None else "",
        status=latest.status if latest is not None else "update",
        posted_at=latest.posted_at if latest is not None else incident.starts_at,
        starts_at=incident.starts_at,
        ends_at=incident.ends_at,
        service_name=component.name if component is not None else None,
        service_slug=component.slug if component is not None else None,
    )


def _day_incidents_for(
    incidents: list[Incident],
    day: date,
    *,
    component_id: UUID | None = None,
    component_ids: set[UUID] | None = None,
    now: datetime | None = None,
) -> list[PublicDayIncident]:
    current = now or datetime.now(UTC)
    result: list[PublicDayIncident] = []
    for incident in incidents:
        if not _incident_matches_scope(
            incident,
            component_id=component_id,
            component_ids=component_ids,
        ):
            continue
        if not _incident_overlaps_day(incident, day, now=current):
            continue
        result.append(_public_day_incident(incident, day))
    return result


def _add_stats(left: DayCheckStats, right: DayCheckStats) -> DayCheckStats:
    return DayCheckStats(
        downtime_seconds=left.downtime_seconds + right.downtime_seconds,
        total=left.total + right.total,
        up=left.up + right.up,
        degraded=left.degraded + right.degraded,
        failed=left.failed + right.failed,
    )


def _sum_stats(
    component_ids: list[UUID],
    day_keys: list[date],
    day_stats_by_component_day: dict[tuple[UUID, date], DayCheckStats],
) -> DayCheckStats:
    total = empty_day_stats()
    for component_id in component_ids:
        for day in day_keys:
            stats = day_stats_by_component_day.get((component_id, day))
            if stats is not None and stats.total:
                total = _add_stats(total, stats)
    return total


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
        return max(prev_events, key=lambda item: item[0])[1]
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
            day_status = status_from_stats(stats)
            project_day_statuses[day] = _merge_status(project_day_statuses[day], day_status)
    return project_day_statuses


def _merge_status(current: str, incoming: str) -> str:
    if STATUS_PRIORITY[incoming] > STATUS_PRIORITY[current]:
        return incoming
    return current


def _build_day_bar(
    *,
    day: date,
    day_status: str,
    stats: DayCheckStats,
    incidents: list[PublicDayIncident],
) -> PublicDayBar:
    total = stats.total
    up = stats.up
    degraded = stats.degraded
    failed = stats.failed
    availability = availability_from_stats(stats)

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
        downtime_seconds=stats.downtime_seconds,
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
