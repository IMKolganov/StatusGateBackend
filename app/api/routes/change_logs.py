from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_access_roles
from app.cqrs.common import PaginationParams
from app.schemas.change_log import EntityChangeLogResponse, PaginatedEntityChangeLogResponse
from app.services.change_log_service import ChangeLogService

router = APIRouter(prefix="/api/admin/change-logs", tags=["admin-change-logs"])


def get_change_log_service(db: Session = Depends(get_db)) -> Generator[ChangeLogService, None, None]:
    yield ChangeLogService(db)


@router.get("", response_model=PaginatedEntityChangeLogResponse)
def list_change_logs(
    project_id: UUID | None = None,
    batch_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: ChangeLogService = Depends(get_change_log_service),
) -> PaginatedEntityChangeLogResponse:
    result = service.list(
        project_id=project_id,
        batch_id=batch_id,
        entity_type=entity_type,
        entity_id=entity_id,
        params=PaginationParams(offset=offset, limit=limit),
    )
    return PaginatedEntityChangeLogResponse(
        items=[EntityChangeLogResponse.model_validate(item) for item in result.items],
        total=result.total,
        offset=result.offset,
        limit=result.limit,
    )
