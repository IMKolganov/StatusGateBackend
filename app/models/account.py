from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, ForeignKey, String, Table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

account_roles_table = Table(
    "account_roles",
    Base.metadata,
    Column("account_id", PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "access_role_id",
        PG_UUID(as_uuid=True),
        ForeignKey("access_roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Account(BaseModel[UUID]):
    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    email_verification_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    access_roles: Mapped[list["AccessRole"]] = relationship(
        secondary=account_roles_table,
        back_populates="accounts",
        lazy="selectin",
    )
