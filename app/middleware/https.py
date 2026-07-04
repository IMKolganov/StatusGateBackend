from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.schemas.api_response import ApiResponse


class RequireHttpsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not settings.require_https:
            return await call_next(request)

        if request.url.path in {"/health"}:
            return await call_next(request)

        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if proto != "https":
            payload = ApiResponse.error_response(
                "HTTPS is required",
                status_code=403,
                detail="HTTPS is required",
                trace_id=getattr(request.state, "trace_id", None),
            ).model_dump()
            return JSONResponse(status_code=403, content=payload)
        return await call_next(request)
