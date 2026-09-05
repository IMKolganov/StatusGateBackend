from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.queries.base import BaseQueryHandler
from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.repositories.incident import IncidentRepository, IncidentUpdateRepository


class IncidentQueryHandler(BaseQueryHandler[Incident, UUID, IncidentRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, IncidentRepository(session))

    def get_by_id_with_relations(self, incident_id: UUID) -> Incident | None:
        return self.repository.get_by_id_with_relations(incident_id)

    def list_by_project(self, project_id: UUID) -> list[Incident]:
        return self.repository.list_by_project(project_id)


class IncidentUpdateQueryHandler(BaseQueryHandler[IncidentUpdate, UUID, IncidentUpdateRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, IncidentUpdateRepository(session))
