from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.repositories.base import Repository


class ProjectRepository(Repository[Project, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Project)

    def get_by_slug(self, slug: str) -> Project | None:
        stmt = select(Project).where(Project.slug == slug)
        return self.session.scalar(stmt)

    def list_active(self) -> list[Project]:
        stmt = select(Project).where(Project.is_active.is_(True)).order_by(Project.name)
        return list(self.session.scalars(stmt).all())
