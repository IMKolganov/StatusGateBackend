from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TwoFactorVerifyRequest(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=6)


class TwoFactorEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class TwoFactorDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=6)


class LinkPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class RegistrationStatusResponse(BaseModel):
    allow_registration: bool
    require_email_verification: bool


class MfaRequiredResponse(BaseModel):
    mfa_required: bool = True
    mfa_token: str


class AccountResponse(BaseModel):
    id: str
    email: str
    full_name: str | None
    access_roles: list[str]
    is_totp_enabled: bool
    has_password: bool
    has_google: bool


class TwoFactorSetupResponse(BaseModel):
    secret: str
    otpauth_url: str
    qr_code_base64: str
