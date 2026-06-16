from fastapi import APIRouter, Depends

from app.api.deps import get_auth_service, require_access_roles
from app.api.routes import accounts, auth, catalog, incidents, monitoring, public_status
from app.models.account import Account
from app.schemas.dashboard import AdminDashboardResponse
from app.services.auth_service import AuthService

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(public_status.router)
api_router.include_router(catalog.router)
api_router.include_router(incidents.router)
api_router.include_router(monitoring.router)
api_router.include_router(accounts.router)


@api_router.get("/api/admin/dashboard", response_model=AdminDashboardResponse, tags=["admin"])
def admin_dashboard(
    account: Account = Depends(require_access_roles("admin", "operator", "viewer")),
    auth_service: AuthService = Depends(get_auth_service),
) -> AdminDashboardResponse:
    return AdminDashboardResponse(
        message="Welcome to StatusGate admin",
        account=auth_service.to_account_response(account),
    )
