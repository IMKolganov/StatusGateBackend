from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_SECRETS = {
    "change-me",
    "change-me-in-production",
    "change-me-in-production-use-long-random-string",
    "secret",
    "jwt-secret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://statusgate:statusgate@localhost:5432/statusgate"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    frontend_url: str = "http://localhost:5173"

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    mfa_token_expire_minutes: int = 5

    allow_registration: bool = True
    require_email_verification: bool = False
    require_https: bool = False

    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    access_cookie_name: str = "sg_access_token"
    refresh_cookie_name: str = "sg_refresh_token"

    auth_login_rate_limit: str = "5/minute"

    default_poll_interval_seconds: int = 60
    scheduler_interval_seconds: int = 30

    google_client_id: str = ""
    google_client_secret: str = ""

    totp_issuer: str = "StatusGate"

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in INSECURE_JWT_SECRETS:
            raise ValueError("JWT_SECRET must not use a default or insecure value")
        if len(value.strip()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value.strip()

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_client_id.strip() and self.google_client_secret.strip())


settings = Settings()
