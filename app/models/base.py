from datetime import datetime
from typing import Generic, TypeVar

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IdT = TypeVar("IdT")
ModelT = TypeVar("ModelT")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BaseModel(Base, TimestampMixin, Generic[IdT]):
    """Abstract base for all entities with typed id and audit timestamps."""

    __abstract__ = True

    id: Mapped[IdT]
