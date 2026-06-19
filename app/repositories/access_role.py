from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.access_role import AccessRole
from app.repositories.base import Repository


class AccessRoleRepository(Repository[AccessRole, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AccessRole)

    def get_by_slug(self, slug: str) -> AccessRole | None:
        stmt = select(AccessRole).where(AccessRole.slug == slug)
        return self.session.scalar(stmt)
