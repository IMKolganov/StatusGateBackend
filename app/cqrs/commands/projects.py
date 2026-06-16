from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.project import Project
from app.repositories.project import ProjectRepository


class ProjectCommandHandler(BaseCommandHandler[Project, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProjectRepository(session))
