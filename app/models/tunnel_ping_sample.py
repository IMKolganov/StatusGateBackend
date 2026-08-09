from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.models.monitored_component import MonitoredComponent

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

GATEWAY_TARGET = "gateway"
INTERNET_TARGET = "internet"


class TunnelPingSample(BaseModel[UUID]):
    """Per-minute aggregate of the continuous in-tunnel pinger.

    One row per (component, target, minute). `gateway` samples the first VPN hop,
    `internet` samples a host beyond the exit (catches degradation past the
    gateway that short check-cycle pings miss).
    """

    __tablename__ = "tunnel_ping_samples"
    __table_args__ = (
        UniqueConstraint(
            "monitored_component_id",
            "target",
            "bucket_start",
            name="uq_tunnel_ping_samples_component_target_bucket",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    monitored_component_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(20), nullable=False)
    target_host: Mapped[str | None] = mapped_column(String(64), nullable=True)
    samples_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    samples_received: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    jitter_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    monitored_component: Mapped["MonitoredComponent"] = relationship(back_populates="tunnel_ping_samples")
