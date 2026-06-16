from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.base import BaseQueryHandler
from app.models.monitored_component import MonitoredComponent
from app.repositories.monitored_component import MonitoredComponentRepository


class MonitoredComponentQueryHandler(BaseQueryHandler[MonitoredComponent, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, MonitoredComponentRepository(session))

    def get_by_id_with_relations(self, component_id: UUID) -> MonitoredComponent | None:
        return self.repository.get_by_id_with_relations(component_id)

    def get_by_project_and_slug(self, project_id: UUID, slug: str) -> MonitoredComponent | None:
        return self.repository.get_by_project_and_slug(project_id, slug)

    def list_by_project_paginated(
        self,
        project_id: UUID,
        *,
        active_only: bool = False,
        params: PaginationParams | None = None,
    ) -> PaginatedResult[MonitoredComponent]:
        pagination = params or PaginationParams()
        filters = [MonitoredComponent.project_id == project_id]
        if active_only:
            filters.append(MonitoredComponent.is_active.is_(True))

        stmt = (
            select(MonitoredComponent)
            .where(*filters)
            .order_by(MonitoredComponent.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        count_stmt = select(func.count()).select_from(MonitoredComponent).where(*filters)
        items = list(self._session.scalars(stmt).all())
        total = self._session.scalar(count_stmt) or 0
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )
