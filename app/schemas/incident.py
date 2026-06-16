from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IncidentUpdateCreate(BaseModel):
    message: str = Field(min_length=1)
    status: str = Field(default="update", pattern=r"^(investigating|identified|monitoring|resolved|update)$")
    posted_at: datetime | None = None


class IncidentUpdateUpdate(BaseModel):
    message: str | None = Field(default=None, min_length=1)
    status: str | None = Field(default=None, pattern=r"^(investigating|identified|monitoring|resolved|update)$")
    posted_at: datetime | None = None


class IncidentUpdateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    incident_id: UUID
    message: str
    status: str
    posted_at: datetime
    created_at: datetime
    updated_at: datetime


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1)
    status: str = Field(default="investigating", pattern=r"^(investigating|identified|monitoring|resolved|update)$")
    posted_at: datetime | None = None


class IncidentUpdatePayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    updates: list[IncidentUpdateResponse] = []


class PublicHistoryEntry(BaseModel):
    incident_id: UUID
    update_id: UUID
    title: str
    message: str
    status: str
    posted_at: datetime


class PublicHistoryDay(BaseModel):
    date: date
    month_label: str
    day: int
    weekday_label: str
    entries: list[PublicHistoryEntry]


class PublicProjectHistory(BaseModel):
    project_id: UUID
    project_name: str
    project_slug: str
    days: list[PublicHistoryDay]
