from __future__ import annotations

import enum


class CheckOutcome(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    TIMEOUT = "timeout"
    ERROR = "error"


class CheckType(str, enum.Enum):
    HTTP_STATUS = "http_status"
    JSON = "json"
    XML = "xml"
    OPENVPN = "openvpn"
    XRAY = "xray"


class ConnectionMode(str, enum.Enum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class ConnectionEventType(str, enum.Enum):
    TUNNEL_UP = "tunnel_up"
    TUNNEL_DOWN = "tunnel_down"
    RECONNECT = "reconnect"
    CONNECT_FAILED = "connect_failed"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


CONNECTION_EVENT_TYPES = frozenset({item.value for item in ConnectionEventType})


VPN_CHECK_TYPES = frozenset({CheckType.OPENVPN.value, CheckType.XRAY.value})
PERSISTENT_VPN_CHECK_TYPES = frozenset({CheckType.OPENVPN.value})
HTTP_CHECK_TYPES = frozenset({CheckType.HTTP_STATUS.value, CheckType.JSON.value, CheckType.XML.value})


class IncidentUpdateStatus(str, enum.Enum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    UPDATE = "update"
