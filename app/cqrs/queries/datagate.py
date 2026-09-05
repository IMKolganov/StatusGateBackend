from uuid import UUID

from sqlalchemy.orm import Session

from app.models.datagate_integration import DatagateIntegration
from app.repositories.datagate_integration import DatagateIntegrationRepository


class DatagateIntegrationQueryHandler:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = DatagateIntegrationRepository(session)

    @property
    def repository(self) -> DatagateIntegrationRepository:
        return self._repository

    def get_by_project_id(self, project_id: UUID) -> DatagateIntegration | None:
        return self._repository.get_by_project_id(project_id)

    def list_auto_sync_enabled(self) -> list[DatagateIntegration]:
        return self._repository.list_auto_sync_enabled()
