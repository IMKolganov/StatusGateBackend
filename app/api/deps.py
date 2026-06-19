from collections.abc import Generator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.cookies import get_access_token_from_request
from app.database import get_db
from app.models.account import Account
from app.services.auth_service import AuthService

__all__ = ["get_auth_service", "get_current_account", "get_db", "require_access_roles"]


def get_auth_service(db: Session = Depends(get_db)) -> Generator[AuthService, None, None]:
    yield AuthService(db)


def get_current_account(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> Account:
    token = get_access_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return auth_service.get_account_from_access_token(token)


def require_access_roles(*allowed_roles: str):
    def dependency(account: Account = Depends(get_current_account)) -> Account:
        account_roles = {role.slug for role in account.access_roles}
        if not account_roles.intersection(allowed_roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return account

    return dependency
