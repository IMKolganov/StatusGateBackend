from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.monitored_component import MonitoredComponent
from app.repositories.monitored_component import MonitoredComponentRepository


class MonitoredComponentCommandHandler(BaseCommandHandler[MonitoredComponent, UUID, MonitoredComponentRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, MonitoredComponentRepository(session))
