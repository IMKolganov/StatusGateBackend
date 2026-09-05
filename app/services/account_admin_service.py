from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.cqrs.commands.accounts import AccountCommandHandler
from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.access_roles import AccessRoleQueryHandler
from app.cqrs.queries.accounts import AccountQueryHandler
from app.models.account import Account
from app.schemas.account_admin import AccountRolesUpdate


class AccountAdminService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._queries = AccountQueryHandler(session)
        self._commands = AccountCommandHandler(session)
        self._role_queries = AccessRoleQueryHandler(session)

    def list(self, params: PaginationParams | None = None) -> PaginatedResult[Account]:
        return self._queries.list_with_roles_paginated(params)

    def get(self, account_id: UUID) -> Account:
        account = self._queries.get_by_id_with_roles(account_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
        return account

    def set_roles(self, account_id: UUID, payload: AccountRolesUpdate) -> Account:
        account = self.get(account_id)
        roles = []
        for slug in payload.access_roles:
            role = self._role_queries.get_by_slug(slug)
            if role is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown access role: {slug}")
            roles.append(role)
        account.access_roles = roles
        return self._commands.update(account)

    def set_active(self, account_id: UUID, *, is_active: bool) -> Account:
        account = self.get(account_id)
        account.is_active = is_active
        return self._commands.update(account)
