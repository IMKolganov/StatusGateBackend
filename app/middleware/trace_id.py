import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers.setdefault("X-Trace-Id", trace_id)
        return response
