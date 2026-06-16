from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.component_kind import ComponentKind
from app.repositories.base import Repository


class ComponentKindRepository(Repository[ComponentKind, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentKind)

    def get_by_slug(self, slug: str) -> ComponentKind | None:
        stmt = select(ComponentKind).where(ComponentKind.slug == slug)
        return self.session.scalar(stmt)
