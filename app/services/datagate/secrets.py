"""Encrypt DataGate client_secret at rest (Fernet, key from settings)."""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

# Prefix so we can distinguish ciphertext from legacy plaintext rows.
_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    # Prefer dedicated key; fall back to a stable derivation from JWT_SECRET.
    raw = (settings.datagate_credentials_key or "").strip() or settings.jwt_secret
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_client_secret(plaintext: str) -> str:
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_client_secret(stored: str) -> str:
    """Decrypt stored secret. Legacy plaintext values pass through unchanged."""
    if not stored:
        return stored
    if not stored.startswith(_PREFIX):
        return stored
    token = stored[len(_PREFIX) :]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt DataGate client_secret")
        raise ValueError("Stored DataGate client_secret could not be decrypted") from exc


def is_encrypted_secret(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)
