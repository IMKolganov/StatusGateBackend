from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    description: str | None
    uptime_percent: float | None = None


class PublicServiceStatus(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    environment: str | None
    component_kind: str
    status: str = Field(description="Latest check outcome or 'unknown'")
    latency_ms: int | None = None
    checked_at: datetime | None = None
    network_summary: dict | None = Field(
        default=None,
        description="Latest VPN/network probe details (interface, IP, exit IP, etc.)",
    )


class PublicProjectStatus(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    services: list[PublicServiceStatus]


class PublicDayIncident(BaseModel):
    title: str
    message: str
    status: str
    posted_at: datetime


class PublicDayBar(BaseModel):
    date: date
    status: str = Field(description="operational, degraded, outage, or no_data")
    tooltip: str
    incidents: list[PublicDayIncident] = []


class PublicServiceTimeline(BaseModel):
    id: UUID
    name: str
    slug: str
    component_kind: str
    uptime_percent: float | None = None
    days: list[PublicDayBar]


class PublicComponentGroupTimeline(BaseModel):
    name: str
    component_count: int
    uptime_percent: float | None = None
    days: list[PublicDayBar]
    services: list[PublicServiceTimeline]


class PublicActiveAlert(BaseModel):
    title: str
    message: str
    status: str
    since: datetime | None = None


class PublicSystemStatus(BaseModel):
    project_id: UUID
    project_name: str
    project_slug: str
    range_start: date
    range_end: date
    range_label: str
    days: int
    groups: list[PublicComponentGroupTimeline]
    active_alerts: list[PublicActiveAlert] = []
