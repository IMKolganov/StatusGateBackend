from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_access_roles
from app.api.pagination import to_paginated_response
from app.cqrs.common import PaginationParams
from app.schemas.component_kind import (
    ComponentKindCreate,
    ComponentKindResponse,
    ComponentKindUpdate,
)
from app.schemas.monitored_component import (
    MonitoredComponentCreate,
    MonitoredComponentResponse,
    MonitoredComponentUpdate,
)
from app.schemas.pagination import paginated_of
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.catalog_service import ComponentKindService, MonitoredComponentService, ProjectService
from app.services.monitoring_admin_service import MonitoringAdminService

PaginatedProjectResponse = paginated_of(ProjectResponse)
PaginatedComponentKindResponse = paginated_of(ComponentKindResponse)
PaginatedMonitoredComponentResponse = paginated_of(MonitoredComponentResponse)

router = APIRouter(prefix="/api/admin", tags=["admin-catalog"])


def get_project_service(db: Session = Depends(get_db)) -> Generator[ProjectService, None, None]:
    yield ProjectService(db)


def get_component_kind_service(db: Session = Depends(get_db)) -> Generator[ComponentKindService, None, None]:
    yield ComponentKindService(db)


def get_monitored_component_service(db: Session = Depends(get_db)) -> Generator[MonitoredComponentService, None, None]:
    yield MonitoredComponentService(db)


def get_monitoring_admin_service(db: Session = Depends(get_db)) -> Generator[MonitoringAdminService, None, None]:
    yield MonitoringAdminService(db)


@router.get("/projects", response_model=PaginatedProjectResponse)
def list_projects(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: ProjectService = Depends(get_project_service),
):
    result = service.list(PaginationParams(offset=offset, limit=limit))
    return to_paginated_response(result, ProjectResponse.model_validate)


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    _=Depends(require_access_roles("admin", "operator")),
    service: ProjectService = Depends(get_project_service),
):
    return ProjectResponse.model_validate(service.create(payload))


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: ProjectService = Depends(get_project_service),
):
    return ProjectResponse.model_validate(service.get(project_id))


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    _=Depends(require_access_roles("admin", "operator")),
    service: ProjectService = Depends(get_project_service),
):
    return ProjectResponse.model_validate(service.update(project_id, payload))


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: ProjectService = Depends(get_project_service),
):
    service.delete(project_id)


@router.get("/component-kinds", response_model=PaginatedComponentKindResponse)
def list_component_kinds(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: ComponentKindService = Depends(get_component_kind_service),
):
    result = service.list(PaginationParams(offset=offset, limit=limit))
    return to_paginated_response(result, ComponentKindResponse.model_validate)


@router.post("/component-kinds", response_model=ComponentKindResponse, status_code=status.HTTP_201_CREATED)
def create_component_kind(
    payload: ComponentKindCreate,
    _=Depends(require_access_roles("admin", "operator")),
    service: ComponentKindService = Depends(get_component_kind_service),
):
    return ComponentKindResponse.model_validate(service.create(payload))


@router.get("/component-kinds/{kind_id}", response_model=ComponentKindResponse)
def get_component_kind(
    kind_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: ComponentKindService = Depends(get_component_kind_service),
):
    return ComponentKindResponse.model_validate(service.get(kind_id))


@router.patch("/component-kinds/{kind_id}", response_model=ComponentKindResponse)
def update_component_kind(
    kind_id: UUID,
    payload: ComponentKindUpdate,
    _=Depends(require_access_roles("admin", "operator")),
    service: ComponentKindService = Depends(get_component_kind_service),
):
    return ComponentKindResponse.model_validate(service.update(kind_id, payload))


@router.delete("/component-kinds/{kind_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component_kind(
    kind_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: ComponentKindService = Depends(get_component_kind_service),
):
    service.delete(kind_id)


@router.get("/monitored-components", response_model=PaginatedMonitoredComponentResponse)
def list_monitored_components(
    project_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: MonitoredComponentService = Depends(get_monitored_component_service),
    monitoring: MonitoringAdminService = Depends(get_monitoring_admin_service),
):
    params = PaginationParams(offset=offset, limit=limit)
    result = service.list_by_project(project_id, params) if project_id else service.list(params)
    enriched = monitoring.enrich_components(result.items)
    return {
        "items": enriched,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "has_next": result.has_next,
        "has_previous": result.has_previous,
    }


@router.post("/monitored-components", response_model=MonitoredComponentResponse, status_code=status.HTTP_201_CREATED)
def create_monitored_component(
    payload: MonitoredComponentCreate,
    _=Depends(require_access_roles("admin", "operator")),
    service: MonitoredComponentService = Depends(get_monitored_component_service),
    monitoring: MonitoringAdminService = Depends(get_monitoring_admin_service),
):
    return monitoring.enrich_component(service.create(payload), None)


@router.get("/monitored-components/{component_id}", response_model=MonitoredComponentResponse)
def get_monitored_component(
    component_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    monitoring: MonitoringAdminService = Depends(get_monitoring_admin_service),
):
    return monitoring.enrich_component_by_id(component_id)


@router.patch("/monitored-components/{component_id}", response_model=MonitoredComponentResponse)
def update_monitored_component(
    component_id: UUID,
    payload: MonitoredComponentUpdate,
    _=Depends(require_access_roles("admin", "operator")),
    service: MonitoredComponentService = Depends(get_monitored_component_service),
    monitoring: MonitoringAdminService = Depends(get_monitoring_admin_service),
):
    component = service.update(component_id, payload)
    return monitoring.enrich_component_by_id(component.id)


@router.delete("/monitored-components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_monitored_component(
    component_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: MonitoredComponentService = Depends(get_monitored_component_service),
):
    service.delete(component_id)
