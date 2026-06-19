from pydantic import BaseModel

from app.schemas.auth import AccountResponse


class AdminDashboardResponse(BaseModel):
    message: str
    account: AccountResponse
