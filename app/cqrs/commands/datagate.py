from uuid import UUID

from sqlalchemy.orm import Session

from app.models.datagate_integration import DatagateIntegration
from app.repositories.datagate_integration import DatagateIntegrationRepository


class DatagateIntegrationCommandHandler:
    """Write-side handler for DataGate integrations (PK = project_id)."""

    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        self._session = session
        self._repository = DatagateIntegrationRepository(session)
        self._auto_commit = auto_commit

    @property
    def repository(self) -> DatagateIntegrationRepository:
        return self._repository

    def _should_commit(self, commit: bool | None) -> bool:
        return self._auto_commit if commit is None else commit

    def save(self, entity: DatagateIntegration, *, commit: bool | None = None) -> DatagateIntegration:
        self._session.add(entity)
        self._session.flush()
        self._session.refresh(entity)
        if self._should_commit(commit):
            self._session.commit()
        return entity

    def commit(self) -> None:
        self._session.commit()
