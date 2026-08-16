from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.component_group import ComponentGroup
from app.repositories.component_group import ComponentGroupRepository


class ComponentGroupCommandHandler(BaseCommandHandler[ComponentGroup, UUID, ComponentGroupRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentGroupRepository(session))
