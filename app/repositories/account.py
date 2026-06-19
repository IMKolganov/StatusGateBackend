from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.account import Account
from app.repositories.base import Repository


class AccountRepository(Repository[Account, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Account)

    def get_by_id_with_roles(self, account_id: UUID) -> Account | None:
        stmt = select(Account).options(selectinload(Account.access_roles)).where(Account.id == account_id)
        return self.session.scalar(stmt)

    def get_by_email(self, email: str) -> Account | None:
        stmt = select(Account).options(selectinload(Account.access_roles)).where(Account.email == email)
        return self.session.scalar(stmt)

    def get_by_google_sub(self, google_sub: str) -> Account | None:
        stmt = select(Account).options(selectinload(Account.access_roles)).where(Account.google_sub == google_sub)
        return self.session.scalar(stmt)

    def count_all(self) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(Account)
        return self.session.scalar(stmt) or 0

    def assign_access_role(self, account: Account, access_role) -> Account:
        if access_role not in account.access_roles:
            account.access_roles.append(access_role)
            self.session.flush()
        return account
