from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.queries.base import BaseQueryHandler
from app.models.access_role import AccessRole
from app.repositories.access_role import AccessRoleRepository


class AccessRoleQueryHandler(BaseQueryHandler[AccessRole, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AccessRoleRepository(session))

    def get_by_slug(self, slug: str) -> AccessRole | None:
        return self.repository.get_by_slug(slug)
