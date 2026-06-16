from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.queries.base import BaseQueryHandler
from app.models.component_kind import ComponentKind
from app.repositories.component_kind import ComponentKindRepository


class ComponentKindQueryHandler(BaseQueryHandler[ComponentKind, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentKindRepository(session))

    def get_by_slug(self, slug: str) -> ComponentKind | None:
        return self.repository.get_by_slug(slug)
