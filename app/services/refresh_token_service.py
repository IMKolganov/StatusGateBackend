from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import generate_refresh_token, hash_refresh_token
from app.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, account_id: UUID, *, expires_at: datetime) -> tuple[RefreshToken, str]:
        raw_token = generate_refresh_token()
        entity = RefreshToken(
            account_id=account_id,
            token_hash=hash_refresh_token(raw_token),
            expires_at=expires_at,
        )
        self.session.add(entity)
        self.session.flush()
        self.session.refresh(entity)
        return entity, raw_token

    def get_active_by_raw_token(self, raw_token: str) -> RefreshToken | None:
        token_hash = hash_refresh_token(raw_token)
        stmt = select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        return self.session.scalar(stmt)

    def revoke(self, token: RefreshToken, *, replaced_by_id: UUID | None = None) -> None:
        token.revoked_at = datetime.now(UTC)
        token.replaced_by_id = replaced_by_id
        self.session.flush()

    def revoke_all_for_account(self, account_id: UUID) -> None:
        stmt = select(RefreshToken).where(
            RefreshToken.account_id == account_id,
            RefreshToken.revoked_at.is_(None),
        )
        now = datetime.now(UTC)
        for token in self.session.scalars(stmt):
            token.revoked_at = now
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()


class RefreshTokenService:
    def __init__(self, session: Session) -> None:
        self._repo = RefreshTokenRepository(session)
        self._session = session

    def issue_pair(self, account_id: UUID) -> str:
        from app.config import settings

        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
        _, raw_token = self._repo.create(account_id, expires_at=expires_at)
        self._repo.commit()
        return raw_token

    def rotate(self, raw_token: str) -> tuple[UUID, str]:
        from app.config import settings

        current = self._repo.get_active_by_raw_token(raw_token)
        if current is None:
            raise ValueError("Invalid refresh token")

        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
        replacement, new_raw = self._repo.create(current.account_id, expires_at=expires_at)
        self._repo.revoke(current, replaced_by_id=replacement.id)
        self._repo.commit()
        return current.account_id, new_raw

    def revoke(self, raw_token: str) -> None:
        current = self._repo.get_active_by_raw_token(raw_token)
        if current is not None:
            self._repo.revoke(current)
            self._repo.commit()

    def revoke_all(self, account_id: UUID) -> None:
        self._repo.revoke_all_for_account(account_id)
        self._repo.commit()
