from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Incident(BaseModel[UUID]):
    __tablename__ = "incidents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)

    project: Mapped["Project"] = relationship(back_populates="incidents")
    updates: Mapped[list["IncidentUpdate"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentUpdate.posted_at.desc()",
    )
