from pydantic import BaseModel


class HealthStatusResponse(BaseModel):
    status: str
    database: str
    version: str
