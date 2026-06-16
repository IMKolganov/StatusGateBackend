from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.access_role import AccessRole
from app.repositories.access_role import AccessRoleRepository


class AccessRoleCommandHandler(BaseCommandHandler[AccessRole, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AccessRoleRepository(session))
