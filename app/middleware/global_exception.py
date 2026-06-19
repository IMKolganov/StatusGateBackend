import json
import logging
from asyncio import CancelledError
from collections.abc import Callable

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse, Response, StreamingResponse

from app.schemas.api_response import ApiResponse, is_api_response_payload

logger = logging.getLogger(__name__)

SKIP_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json")
SKIP_PATHS = {"/health"}


class GlobalExceptionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        if _should_skip_request(request):
            return await call_next(request)

        try:
            response = await call_next(request)
        except CancelledError as exc:
            return await _handle_cancelled(request, exc)
        except HTTPException as exc:
            return _error_json(request, exc.status_code, _http_exception_message(exc), detail=_http_exception_detail(exc))
        except Exception as exc:
            logger.exception("Unhandled exception occurred.")
            return _map_exception_to_response(request, exc, notify_logged=True)

        if isinstance(response, RedirectResponse):
            return response

        return await _normalize_success_response(request, response)


def register_exception_handlers(app) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if _should_skip_request(request):
            return JSONResponse(status_code=exc.status_code, content={"detail": _http_exception_detail(exc)})
        return _error_json(request, exc.status_code, _http_exception_message(exc), detail=_http_exception_detail(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', []) if part != 'body')}: {error.get('msg')}"
            for error in exc.errors()
        )
        return _error_json(request, 422, "Validation failed.", detail=detail or None)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return _error_json(request, 429, "Too many requests. Please try again later.", detail=str(exc.detail))


def _should_skip_request(request: Request) -> bool:
    path = request.url.path
    if path in SKIP_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in SKIP_PATH_PREFIXES)


async def _normalize_success_response(request: Request, response: Response) -> Response:
    if response.status_code == 204:
        payload = ApiResponse.success_response(None).model_dump()
        return _json_response(payload, status_code=200)

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response

    body = await _read_body(response)
    if not body:
        payload = ApiResponse.success_response(None).model_dump()
        return _json_response(payload, status_code=response.status_code)

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return Response(content=body, status_code=response.status_code, headers=dict(response.headers), media_type=content_type)

    if is_api_response_payload(parsed):
        return _copy_set_cookie_headers(response, _json_response(parsed, status_code=response.status_code))

    if response.status_code >= 400:
        message = _extract_error_message(parsed)
        detail = parsed.get("detail") if isinstance(parsed, dict) else json.dumps(parsed)
        return _error_json(
            request,
            response.status_code,
            message,
            detail=detail if isinstance(detail, str) else json.dumps(detail),
        )

    wrapped = ApiResponse.success_response(parsed).model_dump()
    return _copy_set_cookie_headers(response, _json_response(wrapped, status_code=response.status_code))


def _copy_set_cookie_headers(source: Response, target: Response) -> Response:
    for name, value in source.raw_headers:
        if name.lower() == b"set-cookie":
            target.headers.append("set-cookie", value.decode("latin-1"))
    return target


async def _handle_cancelled(request: Request, exc: CancelledError) -> JSONResponse:
    logger.debug(
        "Request was cancelled. method=%s path=%s trace_id=%s",
        request.method,
        request.url.path,
        request.state.trace_id,
    )
    return _error_json(request, 499, "Request was cancelled.", detail=str(exc) or None)


def _map_exception_to_response(request: Request, exc: Exception, *, notify_logged: bool) -> JSONResponse:
    del notify_logged  # hook for future admin notifications
    if isinstance(exc, HTTPException):
        return _error_json(request, exc.status_code, _http_exception_message(exc), detail=_http_exception_detail(exc))

    status_code, message, detail = 500, "An unexpected error occurred. Please try again later.", _exception_detail(exc)

    if isinstance(exc, ValueError):
        status_code, message, detail = 400, str(exc), str(exc)
    elif isinstance(exc, PermissionError):
        status_code, message, detail = 403, str(exc) or "Forbidden.", str(exc)
    elif isinstance(exc, IntegrityError):
        status_code, message, detail = 409, "A resource already exists with the same key.", _exception_detail(exc)

    return _error_json(request, status_code, message, detail=detail)


def _error_json(request: Request, status_code: int, message: str, *, detail: str | None) -> JSONResponse:
    payload = ApiResponse.error_response(
        message,
        status_code=status_code,
        detail=detail,
        trace_id=getattr(request.state, "trace_id", None),
    ).model_dump()
    return _json_response(payload, status_code=status_code)


def _json_response(payload: dict, *, status_code: int) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=payload)
    trace_id = None
    if isinstance(payload.get("data"), dict):
        trace_id = payload["data"].get("trace_id")
    if trace_id:
        response.headers["X-Trace-Id"] = trace_id
    return response


async def _read_body(response: Response) -> bytes:
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is not None:
        chunks: list[bytes] = []
        async for chunk in body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    if isinstance(response, StreamingResponse):
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    return getattr(response, "body", b"") or b""


def _http_exception_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and "message" in detail:
        return str(detail["message"])
    return "Request failed."


def _http_exception_detail(exc: HTTPException) -> str | None:
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    if isinstance(detail, (dict, list)):
        return json.dumps(detail)
    return str(detail)


def _extract_error_message(parsed) -> str:
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail:
            return "Validation failed."
        if "message" in parsed:
            return str(parsed["message"])
    return "Request failed."


def _exception_detail(exc: Exception) -> str:
    current = exc
    while current.__cause__ is not None:
        current = current.__cause__
    while current.__context__ is not None and current.__context__ is not current.__cause__:
        current = current.__context__
    return str(current)
