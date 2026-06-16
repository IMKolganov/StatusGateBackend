from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any
from uuid import UUID, uuid4

import bcrypt
import jwt

from app.config import settings

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_MFA = "mfa"
TOKEN_TYPE_REFRESH = "refresh"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_refresh_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def generate_refresh_token() -> str:
    return token_urlsafe(48)


def _create_token(*, subject: str, token_type: str, expires_delta: timedelta, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid4()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(account_id: UUID, access_roles: list[str]) -> str:
    return _create_token(
        subject=str(account_id),
        token_type=TOKEN_TYPE_ACCESS,
        expires_delta=timedelta(minutes=settings.jwt_expire_minutes),
        extra={"access_roles": access_roles},
    )


def create_mfa_token(account_id: UUID) -> str:
    return _create_token(
        subject=str(account_id),
        token_type=TOKEN_TYPE_MFA,
        expires_delta=timedelta(minutes=settings.mfa_token_expire_minutes),
    )


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
