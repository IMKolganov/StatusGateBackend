from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.base import BaseQueryHandler
from app.models.account import Account
from app.repositories.account import AccountRepository


class AccountQueryHandler(BaseQueryHandler[Account, UUID, AccountRepository]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AccountRepository(session))

    def get_by_id_with_roles(self, account_id: UUID) -> Account | None:
        return self.repository.get_by_id_with_roles(account_id)

    def get_by_email(self, email: str) -> Account | None:
        return self.repository.get_by_email(email)

    def get_by_google_sub(self, google_sub: str) -> Account | None:
        return self.repository.get_by_google_sub(google_sub)

    def count_all(self) -> int:
        return self.repository.count_all()

    def list_with_roles_paginated(self, params: PaginationParams | None = None) -> PaginatedResult[Account]:
        pagination = params or PaginationParams()
        items = self.repository.list_with_roles(offset=pagination.offset, limit=pagination.limit)
        total = self.repository.count_all()
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )
