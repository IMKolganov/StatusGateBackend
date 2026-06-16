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


class IncidentUpdateStatus(str, enum.Enum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    UPDATE = "update"
