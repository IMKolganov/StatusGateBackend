from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.monitored_component import MonitoredComponent
from app.repositories.base import Repository


class MonitoredComponentRepository(Repository[MonitoredComponent, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, MonitoredComponent)

    def get_by_id_with_relations(self, component_id: UUID) -> MonitoredComponent | None:
        stmt = (
            select(MonitoredComponent)
            .options(
                selectinload(MonitoredComponent.project),
                selectinload(MonitoredComponent.component_kind),
            )
            .where(MonitoredComponent.id == component_id)
        )
        return self.session.scalar(stmt)

    def list_by_project(self, project_id: UUID, *, active_only: bool = False) -> list[MonitoredComponent]:
        stmt = (
            select(MonitoredComponent)
            .where(MonitoredComponent.project_id == project_id)
            .order_by(MonitoredComponent.name)
        )
        if active_only:
            stmt = stmt.where(MonitoredComponent.is_active.is_(True))
        return list(self.session.scalars(stmt).all())

    def get_by_project_and_slug(self, project_id: UUID, slug: str) -> MonitoredComponent | None:
        stmt = select(MonitoredComponent).where(
            MonitoredComponent.project_id == project_id,
            MonitoredComponent.slug == slug,
        )
        return self.session.scalar(stmt)
