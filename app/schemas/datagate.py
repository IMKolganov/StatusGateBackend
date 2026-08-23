from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.datagate.url_validation import validate_datagate_base_url


class DatagateIntegrationUpsert(BaseModel):
    base_url: str = Field(default="https://api.datagateapp.com", max_length=2048)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str | None = Field(default=None, min_length=1)
    monitor_cn_prefix: str = Field(default="statusgate", min_length=1, max_length=100)
    is_enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: str) -> str:
        return validate_datagate_base_url(value)


class DatagateIntegrationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: UUID
    base_url: str
    client_id: str
    client_secret_set: bool
    monitor_cn_prefix: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class DatagateTestResponse(BaseModel):
    ok: bool
    server_count: int
    message: str


class DatagateServerSummary(BaseModel):
    id: int
    server_type: int
    check_type: str
    server_name: str
    api_url: str | None = None
    is_online: bool = False
    is_disabled: bool = False
    tags: list[str] = Field(default_factory=list)
    host: str | None = None
    port: int | None = None
    proto: str | None = None


class DatagateLocalComponentSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    check_type: str
    datagate_server_id: int | None = None
    datagate_common_name: str | None = None


class DatagateMatchedPair(BaseModel):
    server: DatagateServerSummary
    component: DatagateLocalComponentSummary
    name_differs: bool
    suggested_name: str
    endpoint_match: bool
    proto: str | None = None
    already_linked: bool = False
    score: float = 0.0


class DatagatePreviewResponse(BaseModel):
    matched: list[DatagateMatchedPair]
    new_servers: list[DatagateServerSummary]
    unmatched_local: list[DatagateLocalComponentSummary]
    sync_names_question: str | None = None


class DatagateImportRequest(BaseModel):
    sync_names: bool = True
    refresh_configs: bool = True
    import_new: bool = True
    server_ids: list[int] | None = None


class DatagateImportItemResult(BaseModel):
    server_id: int
    server_name: str
    action: str
    component_id: UUID | None = None
    message: str | None = None


class DatagateImportResponse(BaseModel):
    items: list[DatagateImportItemResult]
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
