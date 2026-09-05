from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.cqrs.commands.incidents import IncidentCommandHandler, IncidentUpdateCommandHandler
from app.cqrs.queries.incidents import IncidentQueryHandler, IncidentUpdateQueryHandler
from app.cqrs.queries.monitored_components import MonitoredComponentQueryHandler
from app.cqrs.queries.projects import ProjectQueryHandler
from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.schemas.incident import (
    IncidentCreate,
    IncidentResponse,
    IncidentUpdateCreate,
    IncidentUpdatePayload,
    IncidentUpdateResponse,
    IncidentUpdateUpdate,
    PublicHistoryDay,
    PublicHistoryEntry,
    PublicProjectHistory,
)


class IncidentService:
    def __init__(self, session: Session, *, display_tz: ZoneInfo | None = None) -> None:
        self._session = session
        self._display_tz = display_tz or ZoneInfo("UTC")
        self._projects = ProjectQueryHandler(session)
        self._components = MonitoredComponentQueryHandler(session)
        self._incident_queries = IncidentQueryHandler(session)
        self._incident_commands = IncidentCommandHandler(session, auto_commit=False)
        self._update_queries = IncidentUpdateQueryHandler(session)
        self._update_commands = IncidentUpdateCommandHandler(session, auto_commit=False)

    def _get_project(self, project_id: UUID) -> Project:
        project = self._projects.get_by_id(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def _get_project_by_slug(self, slug: str) -> Project:
        project = self._session.scalar(select(Project).where(Project.slug == slug, Project.is_active.is_(True)))
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def _get_incident(self, incident_id: UUID) -> Incident:
        incident = self._incident_queries.get_by_id_with_relations(incident_id)
        if incident is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
        return incident

    def _resolve_component(self, project_id: UUID, component_id: UUID | None) -> MonitoredComponent | None:
        if component_id is None:
            return None
        component = self._components.get_by_id(component_id)
        if component is None or component.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Service not found in this project",
            )
        return component

    def list_for_project(self, project_id: UUID) -> list[IncidentResponse]:
        self._get_project(project_id)
        incidents = self._incident_queries.list_by_project(project_id)
        return [self._to_incident_response(incident) for incident in incidents]

    def create(self, project_id: UUID, payload: IncidentCreate) -> IncidentResponse:
        self._get_project(project_id)
        self._resolve_component(project_id, payload.monitored_component_id)
        posted_at = payload.posted_at or datetime.now(UTC)
        starts_at = payload.starts_at or posted_at
        ends_at = payload.ends_at
        if ends_at is not None and ends_at < starts_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ends_at must be greater than or equal to starts_at",
            )
        incident = Incident(
            project_id=project_id,
            title=payload.title,
            monitored_component_id=payload.monitored_component_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        incident.updates.append(
            IncidentUpdate(message=payload.message, status=payload.status, posted_at=posted_at)
        )
        self._incident_commands.create(incident, commit=True)
        return self._to_incident_response(self._get_incident(incident.id))

    def update_incident(self, incident_id: UUID, payload: IncidentUpdatePayload) -> IncidentResponse:
        incident = self._get_incident(incident_id)
        if payload.title is not None:
            incident.title = payload.title
        if payload.clear_monitored_component:
            incident.monitored_component_id = None
        elif payload.monitored_component_id is not None:
            self._resolve_component(incident.project_id, payload.monitored_component_id)
            incident.monitored_component_id = payload.monitored_component_id
        if payload.starts_at is not None:
            incident.starts_at = payload.starts_at
        if payload.clear_ends_at:
            incident.ends_at = None
        elif payload.ends_at is not None:
            incident.ends_at = payload.ends_at
        effective_end = incident.ends_at
        if effective_end is not None and effective_end < incident.starts_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ends_at must be greater than or equal to starts_at",
            )
        self._incident_commands.update(incident, commit=True)
        return self._to_incident_response(self._get_incident(incident_id))

    def delete_incident(self, incident_id: UUID) -> None:
        incident = self._get_incident(incident_id)
        self._incident_commands.delete(incident, commit=True)

    def add_update(self, incident_id: UUID, payload: IncidentUpdateCreate) -> IncidentUpdateResponse:
        incident = self._get_incident(incident_id)
        posted_at = payload.posted_at or datetime.now(UTC)
        update = IncidentUpdate(
            incident_id=incident.id,
            message=payload.message,
            status=payload.status,
            posted_at=posted_at,
        )
        self._update_commands.create(update, commit=True)
        return IncidentUpdateResponse.model_validate(update)

    def update_entry(self, update_id: UUID, payload: IncidentUpdateUpdate) -> IncidentUpdateResponse:
        update = self._update_queries.get_by_id(update_id)
        if update is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident update not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(update, key, value)
        self._update_commands.update(update, commit=True)
        return IncidentUpdateResponse.model_validate(update)

    def delete_update(self, update_id: UUID) -> None:
        update = self._update_queries.get_by_id(update_id)
        if update is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident update not found")
        self._update_commands.delete(update, commit=True)

    def get_public_history(self, slug: str, *, limit: int = 200) -> PublicProjectHistory:
        project = self._get_project_by_slug(slug)
        updates = self._session.scalars(
            select(IncidentUpdate)
            .join(Incident, IncidentUpdate.incident_id == Incident.id)
            .options(
                selectinload(IncidentUpdate.incident).selectinload(Incident.monitored_component),
            )
            .where(Incident.project_id == project.id)
            .order_by(IncidentUpdate.posted_at.desc())
            .limit(limit)
        ).all()

        days_map: dict[date, list[PublicHistoryEntry]] = {}
        day_order: list[date] = []

        for update in updates:
            incident = update.incident
            local_dt = self._as_local(update.posted_at)
            day_key = local_dt.date()
            if day_key not in days_map:
                days_map[day_key] = []
                day_order.append(day_key)
            component = incident.monitored_component
            days_map[day_key].append(
                PublicHistoryEntry(
                    incident_id=incident.id,
                    update_id=update.id,
                    title=incident.title,
                    message=update.message,
                    status=update.status,
                    posted_at=update.posted_at,
                    starts_at=incident.starts_at,
                    ends_at=incident.ends_at,
                    service_name=component.name if component is not None else None,
                    service_slug=component.slug if component is not None else None,
                )
            )

        days = [
            PublicHistoryDay(
                date=day_key,
                month_label=day_key.strftime("%B"),
                day=day_key.day,
                weekday_label=day_key.strftime("%a"),
                entries=days_map[day_key],
            )
            for day_key in day_order
        ]

        return PublicProjectHistory(
            project_id=project.id,
            project_name=project.name,
            project_slug=project.slug,
            days=days,
        )

    def _as_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self._display_tz)

    @staticmethod
    def _to_incident_response(incident: Incident) -> IncidentResponse:
        sorted_updates = sorted(incident.updates, key=lambda item: item.posted_at, reverse=True)
        component = incident.monitored_component
        return IncidentResponse(
            id=incident.id,
            project_id=incident.project_id,
            title=incident.title,
            monitored_component_id=incident.monitored_component_id,
            service_name=component.name if component is not None else None,
            service_slug=component.slug if component is not None else None,
            starts_at=incident.starts_at,
            ends_at=incident.ends_at,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
            updates=[IncidentUpdateResponse.model_validate(item) for item in sorted_updates],
        )
