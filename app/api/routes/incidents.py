from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_access_roles
from app.schemas.incident import (
    IncidentCreate,
    IncidentResponse,
    IncidentUpdateCreate,
    IncidentUpdatePayload,
    IncidentUpdateResponse,
    IncidentUpdateUpdate,
)
from app.services.incident_service import IncidentService

router = APIRouter(prefix="/api/admin", tags=["admin-incidents"])


def get_incident_service(db: Session = Depends(get_db)) -> Generator[IncidentService, None, None]:
    yield IncidentService(db)


@router.get("/projects/{project_id}/incidents", response_model=list[IncidentResponse])
def list_project_incidents(
    project_id: UUID,
    _=Depends(require_access_roles("admin", "operator", "viewer")),
    service: IncidentService = Depends(get_incident_service),
) -> list[IncidentResponse]:
    return service.list_for_project(project_id)


@router.post("/projects/{project_id}/incidents", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_project_incident(
    project_id: UUID,
    payload: IncidentCreate,
    _=Depends(require_access_roles("admin", "operator")),
    service: IncidentService = Depends(get_incident_service),
) -> IncidentResponse:
    return service.create(project_id, payload)


@router.patch("/incidents/{incident_id}", response_model=IncidentResponse)
def update_incident(
    incident_id: UUID,
    payload: IncidentUpdatePayload,
    _=Depends(require_access_roles("admin", "operator")),
    service: IncidentService = Depends(get_incident_service),
) -> IncidentResponse:
    return service.update_incident(incident_id, payload)


@router.delete("/incidents/{incident_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_incident(
    incident_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: IncidentService = Depends(get_incident_service),
) -> None:
    service.delete_incident(incident_id)


@router.post("/incidents/{incident_id}/updates", response_model=IncidentUpdateResponse, status_code=status.HTTP_201_CREATED)
def add_incident_update(
    incident_id: UUID,
    payload: IncidentUpdateCreate,
    _=Depends(require_access_roles("admin", "operator")),
    service: IncidentService = Depends(get_incident_service),
) -> IncidentUpdateResponse:
    return service.add_update(incident_id, payload)


@router.patch("/incident-updates/{update_id}", response_model=IncidentUpdateResponse)
def update_incident_entry(
    update_id: UUID,
    payload: IncidentUpdateUpdate,
    _=Depends(require_access_roles("admin", "operator")),
    service: IncidentService = Depends(get_incident_service),
) -> IncidentUpdateResponse:
    return service.update_entry(update_id, payload)


@router.delete("/incident-updates/{update_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_incident_entry(
    update_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: IncidentService = Depends(get_incident_service),
) -> None:
    service.delete_update(update_id)
