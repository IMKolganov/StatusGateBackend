from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.datagate_integration import DatagateIntegration


class DatagateIntegrationRepository:
    """Repository for per-project DataGate credentials (PK = project_id)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_project_id(self, project_id: UUID) -> DatagateIntegration | None:
        return self.session.get(DatagateIntegration, project_id)

    def list_auto_sync_enabled(self) -> list[DatagateIntegration]:
        stmt = select(DatagateIntegration).where(
            DatagateIntegration.is_enabled.is_(True),
            DatagateIntegration.auto_sync_enabled.is_(True),
        )
        return list(self.session.scalars(stmt).all())
