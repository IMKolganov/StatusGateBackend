from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.base import BaseQueryHandler
from app.models.component_group import ComponentGroup
from app.repositories.component_group import ComponentGroupRepository


class ComponentGroupQueryHandler(BaseQueryHandler[ComponentGroup, UUID, ComponentGroupRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentGroupRepository(session))

    def get_by_project_and_slug(self, project_id: UUID, slug: str) -> ComponentGroup | None:
        return self.repository.get_by_project_and_slug(project_id, slug)

    def list_by_project_paginated(
        self,
        project_id: UUID,
        params: PaginationParams | None = None,
    ) -> PaginatedResult[ComponentGroup]:
        pagination = params or PaginationParams()
        items = self.repository.list_by_project(
            project_id,
            offset=pagination.offset,
            limit=pagination.limit,
        )
        total = self.repository.count_by_project(project_id)
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )
