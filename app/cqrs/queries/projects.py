from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.base import BaseQueryHandler
from app.models.project import Project
from app.repositories.project import ProjectRepository


class ProjectQueryHandler(BaseQueryHandler[Project, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProjectRepository(session))

    def get_by_slug(self, slug: str) -> Project | None:
        return self.repository.get_by_slug(slug)

    def list_active_paginated(self, params: PaginationParams | None = None) -> PaginatedResult[Project]:
        pagination = params or PaginationParams()
        stmt = (
            select(Project)
            .where(Project.is_active.is_(True))
            .order_by(Project.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        count_stmt = select(func.count()).select_from(Project).where(Project.is_active.is_(True))
        items = list(self._session.scalars(stmt).all())
        total = self._session.scalar(count_stmt) or 0
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )
