from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_access_roles
from app.api.pagination import to_paginated_response
from app.cqrs.common import PaginationParams
from app.schemas.monitoring import (
    CheckResultResponse,
    MonitoringSettingsResponse,
    MonitoringSettingsUpdate,
    PurgeCheckHistoryResponse,
    SpeedTestAdvisoryResponse,
)
from app.schemas.monitored_component import MonitoredComponentResponse
from app.schemas.pagination import paginated_of
from app.services.monitoring_admin_service import MonitoringAdminService

PaginatedCheckResultResponse = paginated_of(CheckResultResponse)

router = APIRouter(prefix="/api/admin/monitoring", tags=["admin-monitoring"])


def get_monitoring_admin_service(db: Session = Depends(get_db)) -> MonitoringAdminService:
    return MonitoringAdminService(db)


@router.get("/settings", response_model=MonitoringSettingsResponse)
def get_monitoring_settings(
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
) -> MonitoringSettingsResponse:
    return service.get_settings_response()


@router.patch("/settings", response_model=MonitoringSettingsResponse)
def update_monitoring_settings(
    payload: MonitoringSettingsUpdate,
    _=Depends(require_access_roles("admin", "operator")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
) -> MonitoringSettingsResponse:
    return service.update_settings(payload)


@router.get("/speed-test-advisory", response_model=SpeedTestAdvisoryResponse)
def get_speed_test_advisory(
    project_id: UUID | None = Query(default=None),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
) -> SpeedTestAdvisoryResponse:
    return service.get_speed_test_advisory(project_id)


@router.post("/monitored-components/{component_id}/check", response_model=CheckResultResponse)
def run_manual_check(
    component_id: UUID,
    _=Depends(require_access_roles("admin", "operator")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
) -> CheckResultResponse:
    return service.run_manual_check(component_id)


@router.get("/monitored-components/{component_id}/check-results", response_model=PaginatedCheckResultResponse)
def list_check_results(
    component_id: UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
):
    result = service.list_check_results(component_id, PaginationParams(offset=offset, limit=limit))
    return to_paginated_response(result, CheckResultResponse.model_validate)


@router.delete(
    "/monitored-components/{component_id}/check-results",
    response_model=PurgeCheckHistoryResponse,
)
def purge_check_history(
    component_id: UUID,
    keep: int = Query(0, ge=0, le=10_000),
    _=Depends(require_access_roles("admin", "operator")),
    service: MonitoringAdminService = Depends(get_monitoring_admin_service),
) -> PurgeCheckHistoryResponse:
    return service.purge_check_history(component_id, keep=keep)
