from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.deps import get_auth_service, get_current_account, get_db
from app.auth.cookies import clear_auth_cookies, get_refresh_token_from_request, set_auth_cookies
from app.auth.google_token import verify_google_id_token
from app.config import settings
from app.cqrs.queries.accounts import AccountQueryHandler
from app.models.account import Account
from app.schemas.auth import (
    AccountResponse,
    GoogleLoginRequest,
    LinkPasswordRequest,
    LoginRequest,
    RegistrationStatusResponse,
    MfaRequiredResponse,
    RegisterRequest,
    TwoFactorDisableRequest,
    TwoFactorEnableRequest,
    TwoFactorSetupResponse,
    TwoFactorVerifyRequest,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)


def _auth_json_response(account: Account, auth_service: AuthService, access_token: str, refresh_token: str) -> JSONResponse:
    from app.schemas.api_response import ApiResponse

    payload = ApiResponse.success_response(auth_service.to_account_response(account).model_dump()).model_dump()
    response = JSONResponse(content=payload)
    set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
    return response


def _upsert_google_user(
    userinfo: dict[str, str | None],
    auth_service: AuthService,
) -> tuple[Account, str | None, str | None]:
    return auth_service.upsert_google_account(
        google_sub=userinfo["sub"],
        email=userinfo["email"],
        full_name=userinfo.get("name"),
        avatar_url=userinfo.get("picture"),
    )


@router.get("/registration-status", response_model=RegistrationStatusResponse)
def registration_status(db=Depends(get_db)) -> RegistrationStatusResponse:
    count = AccountQueryHandler(db).count_all()
    return RegistrationStatusResponse(
        allow_registration=settings.allow_registration or count == 0,
        require_email_verification=settings.require_email_verification,
        google_oauth_enabled=settings.google_oauth_enabled,
        google_client_id=settings.google_client_id if settings.google_oauth_enabled else "",
    )


@router.post("/register", response_model=AccountResponse)
def register(payload: RegisterRequest, auth_service: AuthService = Depends(get_auth_service)) -> AccountResponse:
    account = auth_service.register(email=payload.email, password=payload.password, full_name=payload.full_name)
    return auth_service.to_account_response(account)


@router.post("/login")
@limiter.limit(settings.auth_login_rate_limit)
def login(
    request: Request,
    payload: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    account, access_or_mfa, refresh_token = auth_service.authenticate_password(email=payload.email, password=payload.password)
    if account.is_totp_enabled and access_or_mfa and refresh_token is None:
        return MfaRequiredResponse(mfa_token=access_or_mfa)
    if access_or_mfa and refresh_token:
        return _auth_json_response(account, auth_service, access_or_mfa, refresh_token)
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication failed")


@router.post("/login/2fa")
@limiter.limit(settings.auth_login_rate_limit)
def login_2fa(
    request: Request,
    payload: TwoFactorVerifyRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    access_token, refresh_token = auth_service.verify_mfa(mfa_token=payload.mfa_token, code=payload.code)
    account = auth_service.get_account_from_access_token(access_token)
    return _auth_json_response(account, auth_service, access_token, refresh_token)


@router.post("/refresh")
def refresh_session(request: Request, auth_service: AuthService = Depends(get_auth_service)):
    raw_refresh = get_refresh_token_from_request(request)
    if not raw_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token missing")
    access_token, refresh_token = auth_service.refresh_session(raw_refresh)
    account = auth_service.get_account_from_access_token(access_token)
    return _auth_json_response(account, auth_service, access_token, refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, auth_service: AuthService = Depends(get_auth_service)):
    raw_refresh = get_refresh_token_from_request(request)
    from app.auth.cookies import get_access_token_from_request

    account_id = None
    access_token = get_access_token_from_request(request)
    if access_token:
        try:
            account_id = auth_service.get_account_from_access_token(access_token).id
        except HTTPException:
            account_id = None
    auth_service.logout(account_id=account_id, raw_refresh_token=raw_refresh)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(response)
    return response


@router.get("/me", response_model=AccountResponse)
def me(
    account: Account = Depends(get_current_account),
    auth_service: AuthService = Depends(get_auth_service),
) -> AccountResponse:
    return auth_service.to_account_response(account)


@router.post("/password/link", response_model=AccountResponse)
def link_password(
    payload: LinkPasswordRequest,
    account: Account = Depends(get_current_account),
    auth_service: AuthService = Depends(get_auth_service),
) -> AccountResponse:
    updated = auth_service.link_password(account, payload.password)
    return auth_service.to_account_response(updated)


@router.post("/google-login")
def google_login(
    payload: GoogleLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    if not settings.google_oauth_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth is not configured")

    userinfo = verify_google_id_token(payload.id_token)
    account, access_or_mfa, refresh_token = _upsert_google_user(userinfo, auth_service)

    if account.is_totp_enabled and access_or_mfa and refresh_token is None:
        return MfaRequiredResponse(mfa_token=access_or_mfa)
    if access_or_mfa and refresh_token:
        return _auth_json_response(account, auth_service, access_or_mfa, refresh_token)
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication failed")


@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
def setup_2fa(
    account: Account = Depends(get_current_account),
    auth_service: AuthService = Depends(get_auth_service),
) -> TwoFactorSetupResponse:
    secret, otpauth_url, qr_code_base64 = auth_service.setup_totp(account)
    return TwoFactorSetupResponse(secret=secret, otpauth_url=otpauth_url, qr_code_base64=qr_code_base64)


@router.post("/2fa/enable", response_model=AccountResponse)
def enable_2fa(
    payload: TwoFactorEnableRequest,
    account: Account = Depends(get_current_account),
    auth_service: AuthService = Depends(get_auth_service),
) -> AccountResponse:
    updated = auth_service.enable_totp(account, payload.code)
    return auth_service.to_account_response(updated)


@router.post("/2fa/disable", response_model=AccountResponse)
def disable_2fa(
    payload: TwoFactorDisableRequest,
    account: Account = Depends(get_current_account),
    auth_service: AuthService = Depends(get_auth_service),
) -> AccountResponse:
    updated = auth_service.disable_totp(account, password=payload.password, code=payload.code)
    return auth_service.to_account_response(updated)
