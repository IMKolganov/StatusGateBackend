from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_access_roles
from app.models.account import Account
from app.schemas.datagate import (
    DatagateImportRequest,
    DatagateImportResponse,
    DatagateIntegrationResponse,
    DatagateIntegrationUpsert,
    DatagatePreviewResponse,
    DatagateServerSummary,
    DatagateTestResponse,
)
from app.services.audit import audit_scope
from app.services.datagate.import_service import DatagateIntegrationService

router = APIRouter(prefix="/api/admin/projects/{project_id}/datagate", tags=["admin-datagate"])


def get_datagate_service(db: Session = Depends(get_db)) -> Generator[DatagateIntegrationService, None, None]:
    yield DatagateIntegrationService(db)


@router.get("", response_model=DatagateIntegrationResponse | None)
def get_datagate_integration(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> DatagateIntegrationResponse | None:
    integration = service.get_integration(project_id)
    if integration is None:
        return None
    return service.to_response(integration)


@router.put("", response_model=DatagateIntegrationResponse)
def upsert_datagate_integration(
    project_id: UUID,
    payload: DatagateIntegrationUpsert,
    request: Request,
    account: Account = Depends(require_access_roles("admin", "operator")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> DatagateIntegrationResponse:
    trace_id = getattr(request.state, "trace_id", None)
    with audit_scope(source="api", actor_account_id=account.id, trace_id=trace_id):
        return service.to_response(service.upsert(project_id, payload))


@router.post("/test", response_model=DatagateTestResponse)
def test_datagate_connection(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> DatagateTestResponse:
    return service.test_connection(project_id)


@router.get("/servers", response_model=list[DatagateServerSummary])
def list_datagate_servers(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> list[DatagateServerSummary]:
    return service.list_servers(project_id)


@router.post("/preview", response_model=DatagatePreviewResponse)
def preview_datagate_import(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> DatagatePreviewResponse:
    return service.preview(project_id)


@router.post("/import", response_model=DatagateImportResponse, status_code=status.HTTP_200_OK)
def import_datagate_servers(
    project_id: UUID,
    payload: DatagateImportRequest,
    request: Request,
    account: Account = Depends(require_access_roles("admin", "operator")),
    service: DatagateIntegrationService = Depends(get_datagate_service),
) -> DatagateImportResponse:
    if payload.delete_removed:
        roles = {role.slug for role in account.access_roles}
        if "admin" not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can delete services removed from DataGate",
            )
    trace_id = getattr(request.state, "trace_id", None)
    with audit_scope(source="api", actor_account_id=account.id, trace_id=trace_id):
        return service.import_servers(
            project_id,
            payload,
            source="api",
            actor_account_id=account.id,
            record_sync_status=False,
        )
