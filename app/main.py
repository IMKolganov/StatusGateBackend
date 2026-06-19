from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from slowapi.middleware import SlowAPIMiddleware

from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.api.routes.auth import limiter
from app.config import settings
from app.core.openapi import setup_openapi
from app.database import check_db_connection
from app.middleware.global_exception import GlobalExceptionMiddleware, register_exception_handlers
from app.middleware.https import RequireHttpsMiddleware
from app.middleware.trace_id import TraceIdMiddleware
from app.schemas.health import HealthStatusResponse

app = FastAPI(title="StatusGate API", version="0.1.0")
setup_openapi(app)

app.state.limiter = limiter
register_exception_handlers(app)

app.add_middleware(GlobalExceptionMiddleware)
app.add_middleware(TraceIdMiddleware)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequireHttpsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthStatusResponse)
async def health() -> HealthStatusResponse:
    db_status = "ok"
    try:
        check_db_connection()
    except Exception:
        db_status = "error"

    return HealthStatusResponse(status="ok", database=db_status)
