"""
Coach Auto API — application entrypoint.

Autonomy Health and Fitness · online strength coaching platform.
"""

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import limiter

configure_logging()
log = get_logger("api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    log.info("startup", environment=settings.ENVIRONMENT)

    if settings.SEED_ON_STARTUP:
        from app.services.seed import run_seed

        async with SessionLocal() as db:
            await run_seed(db)

    yield

    await engine.dispose()
    log.info("shutdown")

DOCS_ENABLED = settings.DEBUG or not settings.is_production

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description=(
        "API for Coach Auto — training programmes, meal plans, progress tracking, "
        "sleep and cardio logging for Autonomy Health and Fitness."
    ),
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

# --- Middleware ---------------------------------------------------------------

app.state.limiter = limiter

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Range", "If-Range"],
    expose_headers=["X-Request-ID", "Content-Range", "Accept-Ranges", "Content-Length"],
    max_age=600,
)

if settings.is_production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag every request with an ID, time it, and set security headers."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        log.exception("request.failed", method=request.method)
        structlog.contextvars.clear_contextvars()
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Something went wrong on our side. Try again in a moment."},
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id

    # Common security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    
    # --- CSP Modification Start ---
    docs_paths = {"/docs", "/redoc", "/openapi.json"}
    if request.url.path in docs_paths:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' https://fastapi.tiangolo.com data:; "
            "connect-src 'self' https://cdn.jsdelivr.net "
            "http://127.0.0.1:8000 http://localhost:8000; "
            "frame-ancestors 'none';"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"

    # Private media must never end up in a search index or a shared cache.
    if request.url.path.endswith(("/file", "/stream")):
        response.headers["X-Robots-Tag"] = "noindex, noimageindex, nofollow"
        
    # --- CSP Modification End ---

    if settings.is_production:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains; preload"
        )

    if duration_ms > 500 or response.status_code >= 500:
        log.warning(
            "request.slow_or_failed",
            method=request.method,
            status=response.status_code,
            duration_ms=duration_ms,
        )

    structlog.contextvars.clear_contextvars()
    return response

# --- Error handling -----------------------------------------------------------

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "That is a lot of requests in a short time. Wait a minute and retry."},
    )

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Flatten Pydantic errors into field → message, which is what forms need."""
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = [part for part in error["loc"] if part not in ("body", "query", "path")]
        key = ".".join(str(part) for part in location) or "request"
        fields[key] = error["msg"].removeprefix("Value error, ")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Check the highlighted fields and try again.", "fields": fields},
    )

# --- Routes -------------------------------------------------------------------
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

@app.get("/health", tags=["ops"], include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Deliberately does not touch the database."""
    return {"status": "ok", "service": "coach-auto-api"}

@app.get("/health/ready", tags=["ops"], include_in_schema=False)
async def readiness() -> dict[str, str]:
    """Readiness probe — Coolify points its health check here."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}