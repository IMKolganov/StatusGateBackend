from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Subscription(BaseModel[UUID]):
    """Notification subscription for status updates (email / SMS / webhook)."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NOT NULL AND monitored_component_id IS NULL) OR "
            "(project_id IS NULL AND monitored_component_id IS NOT NULL)",
            name="ck_subscriptions_exactly_one_scope",
        ),
        CheckConstraint(
            "channel IN ('email', 'sms', 'webhook')",
            name="ck_subscriptions_channel",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    destination: Mapped[str] = mapped_column(String(2048), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    monitored_component_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_components.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    verification_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notify_on_incident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    notify_on_resolution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    notify_on_maintenance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
