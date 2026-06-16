from uuid import UUID

from sqlalchemy.orm import Session

from app.cqrs.commands.base import BaseCommandHandler
from app.models.account import Account
from app.repositories.account import AccountRepository


class AccountCommandHandler(BaseCommandHandler[Account, UUID]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AccountRepository(session))

    def assign_access_role(self, account: Account, access_role) -> Account:
        updated = self.repository.assign_access_role(account, access_role)
        self.repository.commit()
        return updated
