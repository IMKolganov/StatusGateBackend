from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.queries.base import BaseQueryHandler
from app.models.account import Account
from app.repositories.account import AccountRepository


class AccountQueryHandler(BaseQueryHandler[Account, UUID]):
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
