from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.models.account import Account

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.account import account_roles_table
from app.models.base import BaseModel


class AccessRole(BaseModel[UUID]):
    __tablename__ = "access_roles"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(
        secondary=account_roles_table,
        back_populates="access_roles",
    )
