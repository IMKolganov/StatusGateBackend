from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.component_kind import ComponentKind
from app.repositories.component_kind import ComponentKindRepository


class ComponentKindCommandHandler(BaseCommandHandler[ComponentKind, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentKindRepository(session))
