from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.repositories.base import Repository


class IncidentRepository(Repository[Incident, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Incident)

    def get_by_id_with_relations(self, incident_id: UUID) -> Incident | None:
        stmt = (
            select(Incident)
            .options(
                selectinload(Incident.updates),
                selectinload(Incident.monitored_component),
            )
            .where(Incident.id == incident_id)
        )
        return self.session.scalar(stmt)

    def list_by_project(self, project_id: UUID) -> list[Incident]:
        stmt = (
            select(Incident)
            .options(
                selectinload(Incident.updates),
                selectinload(Incident.monitored_component),
            )
            .where(Incident.project_id == project_id)
            .order_by(Incident.created_at.desc())
        )
        return list(self.session.scalars(stmt).all())


class IncidentUpdateRepository(Repository[IncidentUpdate, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, IncidentUpdate)
