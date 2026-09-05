from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EntityChangeLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    occurred_at: datetime
    actor_account_id: UUID | None = None
    source: str
    batch_id: UUID | None = None
    entity_type: str
    entity_id: str
    project_id: UUID | None = None
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    trace_id: str | None = None
    request_id: str | None = None
    summary: str | None = None


class PaginatedEntityChangeLogResponse(BaseModel):
    items: list[EntityChangeLogResponse]
    total: int
    offset: int = 0
    limit: int = 50
