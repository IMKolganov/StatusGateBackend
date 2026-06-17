import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from app.config import settings

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(GOOGLE_JWKS_URL)
    return _jwk_client


def verify_google_id_token(credential: str) -> dict[str, str]:
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(credential)
        data = jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=list(GOOGLE_ISSUERS),
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google credential") from exc

    email_verified = data.get("email_verified")
    if email_verified not in (True, "true"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email is not verified")

    sub = data.get("sub")
    email = data.get("email")
    if not sub or not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google profile not available")

    picture = data.get("picture")
    return {
        "sub": str(sub),
        "email": str(email),
        "name": str(data["name"]) if data.get("name") else None,
        "picture": str(picture) if picture else None,
    }
