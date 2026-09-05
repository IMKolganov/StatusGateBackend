from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.repositories.incident import IncidentRepository, IncidentUpdateRepository


class IncidentCommandHandler(BaseCommandHandler[Incident, UUID, IncidentRepository]):
    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        super().__init__(session, IncidentRepository(session), auto_commit=auto_commit)


class IncidentUpdateCommandHandler(BaseCommandHandler[IncidentUpdate, UUID, IncidentUpdateRepository]):
    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        super().__init__(session, IncidentUpdateRepository(session), auto_commit=auto_commit)
