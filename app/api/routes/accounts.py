from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_auth_service, get_db, require_access_roles
from app.api.pagination import to_paginated_response
from app.cqrs.common import PaginationParams
from app.schemas.account_admin import AccountAdminResponse, AccountRolesUpdate
from app.schemas.pagination import paginated_of
from app.services.account_admin_service import AccountAdminService
from app.services.auth_service import AuthService

PaginatedAccountAdminResponse = paginated_of(AccountAdminResponse)

router = APIRouter(prefix="/api/admin/accounts", tags=["admin-accounts"])


def get_account_admin_service(db: Session = Depends(get_db)) -> Generator[AccountAdminService, None, None]:
    yield AccountAdminService(db)


def _to_admin_response(account, auth_service: AuthService) -> AccountAdminResponse:
    base = auth_service.to_account_response(account)
    return AccountAdminResponse(
        id=account.id,
        email=base.email,
        full_name=base.full_name,
        access_roles=base.access_roles,
        is_active=account.is_active,
        is_totp_enabled=base.is_totp_enabled,
        has_password=base.has_password,
        has_google=base.has_google,
        created_at=account.created_at,
    )


@router.get("", response_model=PaginatedAccountAdminResponse)
def list_accounts(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    _=Depends(require_access_roles("admin")),
    service: AccountAdminService = Depends(get_account_admin_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    result = service.list(PaginationParams(offset=offset, limit=limit))
    return to_paginated_response(result, lambda account: _to_admin_response(account, auth_service))


@router.get("/{account_id}", response_model=AccountAdminResponse)
def get_account(
    account_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: AccountAdminService = Depends(get_account_admin_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    return _to_admin_response(service.get(account_id), auth_service)


@router.put("/{account_id}/roles", response_model=AccountAdminResponse)
def update_account_roles(
    account_id: UUID,
    payload: AccountRolesUpdate,
    _=Depends(require_access_roles("admin")),
    service: AccountAdminService = Depends(get_account_admin_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    return _to_admin_response(service.set_roles(account_id, payload), auth_service)


@router.post("/{account_id}/deactivate", response_model=AccountAdminResponse)
def deactivate_account(
    account_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: AccountAdminService = Depends(get_account_admin_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    return _to_admin_response(service.set_active(account_id, is_active=False), auth_service)


@router.post("/{account_id}/activate", response_model=AccountAdminResponse)
def activate_account(
    account_id: UUID,
    _=Depends(require_access_roles("admin")),
    service: AccountAdminService = Depends(get_account_admin_service),
    auth_service: AuthService = Depends(get_auth_service),
):
    return _to_admin_response(service.set_active(account_id, is_active=True), auth_service)
