from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.component_group import ComponentGroup
from app.repositories.base import Repository


class ComponentGroupRepository(Repository[ComponentGroup, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ComponentGroup)

    def get_by_project_and_slug(self, project_id: UUID, slug: str) -> ComponentGroup | None:
        stmt = select(ComponentGroup).where(
            ComponentGroup.project_id == project_id,
            ComponentGroup.slug == slug,
        )
        return self.session.scalar(stmt)

    def list_by_project(self, project_id: UUID, *, offset: int = 0, limit: int = 100) -> list[ComponentGroup]:
        stmt = (
            select(ComponentGroup)
            .where(ComponentGroup.project_id == project_id)
            .order_by(ComponentGroup.sort_order.asc(), ComponentGroup.name.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    def count_by_project(self, project_id: UUID) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(ComponentGroup).where(ComponentGroup.project_id == project_id)
        return self.session.scalar(stmt) or 0
