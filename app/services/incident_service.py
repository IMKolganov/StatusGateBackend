from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
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

    def _get_project(self, project_id: UUID) -> Project:
        project = self._session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def _get_project_by_slug(self, slug: str) -> Project:
        project = self._session.scalar(select(Project).where(Project.slug == slug, Project.is_active.is_(True)))
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def _get_incident(self, incident_id: UUID) -> Incident:
        incident = self._session.scalar(
            select(Incident)
            .options(selectinload(Incident.updates))
            .where(Incident.id == incident_id)
        )
        if incident is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
        return incident

    def list_for_project(self, project_id: UUID) -> list[IncidentResponse]:
        self._get_project(project_id)
        incidents = self._session.scalars(
            select(Incident)
            .options(selectinload(Incident.updates))
            .where(Incident.project_id == project_id)
            .order_by(Incident.created_at.desc())
        ).all()
        return [self._to_incident_response(incident) for incident in incidents]

    def create(self, project_id: UUID, payload: IncidentCreate) -> IncidentResponse:
        self._get_project(project_id)
        posted_at = payload.posted_at or datetime.now(UTC)
        incident = Incident(project_id=project_id, title=payload.title)
        incident.updates.append(
            IncidentUpdate(message=payload.message, status=payload.status, posted_at=posted_at)
        )
        self._session.add(incident)
        self._session.commit()
        self._session.refresh(incident)
        return self._to_incident_response(self._get_incident(incident.id))

    def update_incident(self, incident_id: UUID, payload: IncidentUpdatePayload) -> IncidentResponse:
        incident = self._get_incident(incident_id)
        if payload.title is not None:
            incident.title = payload.title
        self._session.commit()
        return self._to_incident_response(self._get_incident(incident_id))

    def delete_incident(self, incident_id: UUID) -> None:
        incident = self._get_incident(incident_id)
        self._session.delete(incident)
        self._session.commit()

    def add_update(self, incident_id: UUID, payload: IncidentUpdateCreate) -> IncidentUpdateResponse:
        incident = self._get_incident(incident_id)
        posted_at = payload.posted_at or datetime.now(UTC)
        update = IncidentUpdate(
            incident_id=incident.id,
            message=payload.message,
            status=payload.status,
            posted_at=posted_at,
        )
        self._session.add(update)
        self._session.commit()
        self._session.refresh(update)
        return IncidentUpdateResponse.model_validate(update)

    def update_entry(self, update_id: UUID, payload: IncidentUpdateUpdate) -> IncidentUpdateResponse:
        update = self._session.get(IncidentUpdate, update_id)
        if update is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident update not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(update, key, value)
        self._session.commit()
        self._session.refresh(update)
        return IncidentUpdateResponse.model_validate(update)

    def delete_update(self, update_id: UUID) -> None:
        update = self._session.get(IncidentUpdate, update_id)
        if update is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident update not found")
        self._session.delete(update)
        self._session.commit()

    def get_public_history(self, slug: str, *, limit: int = 200) -> PublicProjectHistory:
        project = self._get_project_by_slug(slug)
        rows = self._session.execute(
            select(IncidentUpdate, Incident)
            .join(Incident, IncidentUpdate.incident_id == Incident.id)
            .where(Incident.project_id == project.id)
            .order_by(IncidentUpdate.posted_at.desc())
            .limit(limit)
        ).all()

        days_map: dict[date, list[PublicHistoryEntry]] = {}
        day_order: list[date] = []

        for update, incident in rows:
            local_dt = self._as_local(update.posted_at)
            day_key = local_dt.date()
            if day_key not in days_map:
                days_map[day_key] = []
                day_order.append(day_key)
            days_map[day_key].append(
                PublicHistoryEntry(
                    incident_id=incident.id,
                    update_id=update.id,
                    title=incident.title,
                    message=update.message,
                    status=update.status,
                    posted_at=update.posted_at,
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
        return IncidentResponse(
            id=incident.id,
            project_id=incident.project_id,
            title=incident.title,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
            updates=[IncidentUpdateResponse.model_validate(item) for item in sorted_updates],
        )
