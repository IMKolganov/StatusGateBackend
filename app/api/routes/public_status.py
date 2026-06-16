from collections.abc import Generator

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.incident import PublicProjectHistory
from app.schemas.public_status import PublicProjectStatus, PublicProjectSummary, PublicSystemStatus
from app.services.incident_service import IncidentService
from app.services.public_status_service import PublicStatusService

router = APIRouter(prefix="/api/status", tags=["public-status"])


def get_public_status_service(db: Session = Depends(get_db)) -> Generator[PublicStatusService, None, None]:
    yield PublicStatusService(db)


def get_incident_service(db: Session = Depends(get_db)) -> Generator[IncidentService, None, None]:
    yield IncidentService(db)


@router.get("/projects", response_model=list[PublicProjectSummary])
def list_public_projects(
    limit: int = Query(100, ge=1, le=500),
    service: PublicStatusService = Depends(get_public_status_service),
) -> list[PublicProjectSummary]:
    return service.list_projects(limit=limit)


@router.get("/projects/{slug}", response_model=PublicProjectStatus)
def get_public_project_status(
    slug: str,
    service: PublicStatusService = Depends(get_public_status_service),
) -> PublicProjectStatus:
    return service.get_project_status(slug)


@router.get("/projects/{slug}/history", response_model=PublicProjectHistory)
def get_public_project_history(
    slug: str,
    service: IncidentService = Depends(get_incident_service),
) -> PublicProjectHistory:
    return service.get_public_history(slug)


@router.get("/projects/{slug}/system-status", response_model=PublicSystemStatus)
def get_public_system_status(
    slug: str,
    end: date | None = Query(None, description="Last day of the range (UTC). Defaults to today."),
    days: int = Query(90, ge=7, le=365, description="Number of days in the timeline."),
    service: PublicStatusService = Depends(get_public_status_service),
) -> PublicSystemStatus:
    return service.get_system_status(slug, end=end, days=days)
