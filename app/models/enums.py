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


VPN_CHECK_TYPES = frozenset({CheckType.OPENVPN.value, CheckType.XRAY.value})
HTTP_CHECK_TYPES = frozenset({CheckType.HTTP_STATUS.value, CheckType.JSON.value, CheckType.XML.value})


class IncidentUpdateStatus(str, enum.Enum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    UPDATE = "update"
