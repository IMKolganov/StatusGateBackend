from uuid import UUID

from fastapi import HTTPException, status
from secrets import token_urlsafe
from sqlalchemy.orm import Session

from app.auth.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_MFA,
    create_access_token,
    create_mfa_token,
    hash_password,
    verify_password,
)
from app.auth.totp import build_totp_qr_base64, build_totp_uri, generate_totp_secret, verify_totp_code
from app.config import settings
from app.cqrs.commands.accounts import AccountCommandHandler
from app.cqrs.queries.access_roles import AccessRoleQueryHandler
from app.cqrs.queries.accounts import AccountQueryHandler
from app.models.account import Account
from app.auth.roles import DEFAULT_BOOTSTRAP_ROLE_SLUG, DEFAULT_PUBLIC_ROLE_SLUG
from app.schemas.auth import AccountResponse
from app.services.refresh_token_service import RefreshTokenService


class AuthService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._account_queries = AccountQueryHandler(session)
        self._account_commands = AccountCommandHandler(session)
        self._access_role_queries = AccessRoleQueryHandler(session)
        self._refresh_tokens = RefreshTokenService(session)

    def register(self, *, email: str, password: str, full_name: str | None) -> Account:
        if not settings.allow_registration and self._account_queries.count_all() > 0:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is disabled")

        if self._account_queries.get_by_email(email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

        is_first_account = self._account_queries.count_all() == 0
        role_slug = DEFAULT_BOOTSTRAP_ROLE_SLUG if is_first_account else DEFAULT_PUBLIC_ROLE_SLUG
        access_role = self._access_role_queries.get_by_slug(role_slug)
        if access_role is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Default access role not found")

        account = Account(
            email=email.lower(),
            password_hash=hash_password(password),
            full_name=full_name,
        )
        if settings.require_email_verification:
            account.is_email_verified = False
            account.email_verification_token = token_urlsafe(32)
        else:
            account.is_email_verified = True
            account.email_verification_token = None
        account.access_roles.append(access_role)
        return self._account_commands.create(account)

    def authenticate_password(self, *, email: str, password: str) -> tuple[Account, str | None, str | None]:
        account = self._account_queries.get_by_email(email.lower())
        if account is None or not account.password_hash or not verify_password(password, account.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        if not account.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")

        if settings.require_email_verification and not account.is_email_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Please verify your email before signing in",
            )

        if account.is_totp_enabled and account.totp_secret:
            return account, create_mfa_token(account.id), None

        access_token, refresh_token = self.issue_tokens(account)
        return account, access_token, refresh_token

    def verify_mfa(self, *, mfa_token: str, code: str) -> tuple[str, str]:
        account = self._account_from_token(mfa_token, expected_type=TOKEN_TYPE_MFA)
        if not account.totp_secret or not verify_totp_code(secret=account.totp_secret, code=code):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA code")
        return self.issue_tokens(account)

    def refresh_session(self, raw_refresh_token: str) -> tuple[str, str]:
        try:
            account_id, new_refresh = self._refresh_tokens.rotate(raw_refresh_token)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc

        account = self._account_queries.get_by_id_with_roles(account_id)
        if account is None or not account.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")
        access_token = create_access_token(account.id, self._access_role_slugs(account))
        return access_token, new_refresh

    def logout(self, *, account_id: UUID | None, raw_refresh_token: str | None) -> None:
        if raw_refresh_token:
            self._refresh_tokens.revoke(raw_refresh_token)
        if account_id is not None:
            self._refresh_tokens.revoke_all(account_id)

    def get_account_from_access_token(self, token: str) -> Account:
        return self._account_from_token(token, expected_type=TOKEN_TYPE_ACCESS)

    def link_password(self, account: Account, password: str) -> Account:
        if account.password_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Password is already linked")
        account.password_hash = hash_password(password)
        return self._account_commands.update(account)

    def setup_totp(self, account: Account) -> tuple[str, str, str]:
        secret = generate_totp_secret()
        account.totp_secret = secret
        account.is_totp_enabled = False
        self._account_commands.update(account)
        otpauth_url = build_totp_uri(secret=secret, email=account.email, issuer=settings.totp_issuer)
        qr_code_base64 = build_totp_qr_base64(otpauth_url=otpauth_url)
        return secret, otpauth_url, qr_code_base64

    def enable_totp(self, account: Account, code: str) -> Account:
        if not account.totp_secret:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA setup not started")
        if not verify_totp_code(secret=account.totp_secret, code=code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code")
        account.is_totp_enabled = True
        return self._account_commands.update(account)

    def disable_totp(self, account: Account, *, password: str, code: str) -> Account:
        if not account.password_hash or not verify_password(password, account.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
        if not account.totp_secret or not verify_totp_code(secret=account.totp_secret, code=code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code")
        account.totp_secret = None
        account.is_totp_enabled = False
        return self._account_commands.update(account)

    def upsert_google_account(self, *, google_sub: str, email: str, full_name: str | None) -> tuple[Account, str | None, str | None]:
        account = self._account_queries.get_by_google_sub(google_sub)
        if account is None:
            account = self._account_queries.get_by_email(email.lower())

        if account is None:
            if not settings.allow_registration and self._account_queries.count_all() > 0:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is disabled")

            is_first_account = self._account_queries.count_all() == 0
            role_slug = DEFAULT_BOOTSTRAP_ROLE_SLUG if is_first_account else DEFAULT_PUBLIC_ROLE_SLUG
            access_role = self._access_role_queries.get_by_slug(role_slug)
            if access_role is None:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Default access role not found")

            account = Account(
                email=email.lower(),
                google_sub=google_sub,
                full_name=full_name,
            )
            account.access_roles.append(access_role)
            account = self._account_commands.create(account)
        else:
            account.google_sub = google_sub
            if full_name and not account.full_name:
                account.full_name = full_name
            account = self._account_commands.update(account)

        if not account.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")

        if account.is_totp_enabled and account.totp_secret:
            return account, create_mfa_token(account.id), None

        access_token, refresh_token = self.issue_tokens(account)
        return account, access_token, refresh_token

    def issue_tokens(self, account: Account) -> tuple[str, str]:
        access_token = create_access_token(account.id, self._access_role_slugs(account))
        refresh_token = self._refresh_tokens.issue_pair(account.id)
        return access_token, refresh_token

    @staticmethod
    def to_account_response(account: Account) -> AccountResponse:
        return AccountResponse(
            id=str(account.id),
            email=account.email,
            full_name=account.full_name,
            access_roles=[role.slug for role in account.access_roles],
            is_totp_enabled=account.is_totp_enabled,
            has_password=account.password_hash is not None,
            has_google=account.google_sub is not None,
        )

    def _account_from_token(self, token: str, *, expected_type: str) -> Account:
        from app.auth.security import decode_token

        try:
            payload = decode_token(token)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

        if payload.get("type") != expected_type:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

        account_id = UUID(payload["sub"])
        account = self._account_queries.get_by_id_with_roles(account_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")
        return account

    @staticmethod
    def _access_role_slugs(account: Account) -> list[str]:
        return [role.slug for role in account.access_roles]
