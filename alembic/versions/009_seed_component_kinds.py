"""seed default component kinds: web, openvpn, xray

Revision ID: 009
Revises: 008
Create Date: 2026-06-17

"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | Sequence[str] | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEB_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000020")
OPENVPN_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000021")
XRAY_COMPONENT_KIND_ID = UUID("00000000-0000-4000-8000-000000000022")

DEFAULT_COMPONENT_KINDS = (
    (WEB_COMPONENT_KIND_ID, "Web", "web", "HTTP/HTTPS web endpoints and APIs"),
    (OPENVPN_COMPONENT_KIND_ID, "OpenVPN", "openvpn", "OpenVPN server availability and latency checks"),
    (XRAY_COMPONENT_KIND_ID, "Xray", "xray", "Xray proxy availability and latency checks"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for kind_id, name, slug, description in DEFAULT_COMPONENT_KINDS:
        bind.execute(
            sa.text(
                "INSERT INTO component_kinds (id, name, slug, description) "
                "VALUES (:id, :name, :slug, :description) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": kind_id, "name": name, "slug": slug, "description": description},
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM component_kinds WHERE slug IN ('web', 'openvpn', 'xray')"))
