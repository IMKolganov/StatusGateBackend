from uuid import UUID, uuid4

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel

WEB_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000020")
OPENVPN_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000021")
XRAY_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000022")

DEFAULT_COMPONENT_KINDS: tuple[tuple[UUID, str, str, str], ...] = (
    (WEB_COMPONENT_KIND_ID, "Web", "web", "HTTP/HTTPS web endpoints and APIs"),
    (OPENVPN_COMPONENT_KIND_ID, "OpenVPN", "openvpn", "OpenVPN server availability and latency checks"),
    (XRAY_COMPONENT_KIND_ID, "Xray", "xray", "Xray proxy availability and latency checks"),
)


class ComponentKind(BaseModel[UUID]):
    __tablename__ = "component_kinds"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
