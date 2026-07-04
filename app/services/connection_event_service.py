from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.connection_event import ConnectionEvent
from app.models.enums import CONNECTION_EVENT_TYPES, ConnectionEventType


def record_connection_event(
    session: Session,
    *,
    component_id: UUID,
    event_type: str,
    occurred_at: datetime | None = None,
    outcome: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> ConnectionEvent:
    if event_type not in CONNECTION_EVENT_TYPES:
        raise ValueError(f"Unsupported connection event type: {event_type}")

    event = ConnectionEvent(
        monitored_component_id=component_id,
        occurred_at=occurred_at or datetime.now(UTC),
        event_type=event_type,
        outcome=outcome,
        message=message,
        details=details,
    )
    session.add(event)
    session.flush()
    return event


def connection_event_label(event_type: str) -> str:
    labels = {
        ConnectionEventType.TUNNEL_UP.value: "Connected",
        ConnectionEventType.TUNNEL_DOWN.value: "Disconnected",
        ConnectionEventType.RECONNECT.value: "Reconnecting",
        ConnectionEventType.CONNECT_FAILED.value: "Connect failed",
        ConnectionEventType.UNAVAILABLE.value: "Internet unavailable",
        ConnectionEventType.AVAILABLE.value: "Internet restored",
    }
    return labels.get(event_type, event_type.replace("_", " ").title())
