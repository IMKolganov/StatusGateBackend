from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AccountAdminResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str | None
    access_roles: list[str]
    is_active: bool
    is_totp_enabled: bool
    has_password: bool
    has_google: bool
    created_at: datetime


class AccountRolesUpdate(BaseModel):
    access_roles: list[str] = Field(min_length=1)


class LinkPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
