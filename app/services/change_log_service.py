from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.models.entity_change_log import EntityChangeLog


class ChangeLogService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list(
        self,
        *,
        project_id: UUID | None = None,
        batch_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        params: PaginationParams | None = None,
    ) -> PaginatedResult[EntityChangeLog]:
        pagination = params or PaginationParams(limit=50)
        filters = []
        if project_id is not None:
            filters.append(EntityChangeLog.project_id == project_id)
        if batch_id is not None:
            filters.append(EntityChangeLog.batch_id == batch_id)
        if entity_type:
            filters.append(EntityChangeLog.entity_type == entity_type)
        if entity_id:
            filters.append(EntityChangeLog.entity_id == entity_id)

        count_stmt = select(func.count()).select_from(EntityChangeLog)
        list_stmt = select(EntityChangeLog).order_by(EntityChangeLog.occurred_at.desc())
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)
        total = self._session.scalar(count_stmt) or 0
        items = list(
            self._session.scalars(list_stmt.offset(pagination.offset).limit(pagination.limit)).all()
        )
        return PaginatedResult(items=items, total=total, offset=pagination.offset, limit=pagination.limit)
