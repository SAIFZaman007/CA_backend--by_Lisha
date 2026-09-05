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
from app.core.media import bind_request_origin, reset_request_origin
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


def _public_origin(request: Request) -> str:
    """
    The origin the browser used to reach this API on this request.

    Two front ends on two hostnames share this API, so the address a media file
    should be advertised at depends on who is asking. See `app.core.media` for
    why that is derived rather than configured.

    Two things are deliberately careful here.

    The host is checked against `trusted_hosts` before it is used. A `Host`
    header is attacker-controlled, and an unchecked one would let a caller mint
    signed image URLs pointing at a hostname they chose. An unrecognised host
    yields "", which falls back to root-relative paths — the old behaviour, and
    safe.

    The scheme is not read from `X-Forwarded-Proto` alone. Both SPA nginx
    configs set that header from their own `$scheme`, which is `http` on the
    internal hop even when the browser is on `https`. Trusting it as-is would
    hand an https page an http image URL, which the browser then blocks as
    mixed content — the same silent-broken-image failure by a different route.
    In production this API is only ever reached over https, so https is the
    answer unless a forwarded header positively says otherwise.
    """
    forwarded_host = request.headers.get("x-forwarded-host", "")
    host = (forwarded_host.split(",")[0] or request.headers.get("host", "")).strip()
    if not host:
        return ""

    hostname = host.split(":", 1)[0].lower()
    if hostname not in {h.lower() for h in settings.trusted_hosts}:
        return ""

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if settings.is_production:
        scheme = "https"
    elif forwarded_proto in ("http", "https"):
        scheme = forwarded_proto
    else:
        scheme = request.url.scheme

    return f"{scheme}://{host}"


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag every request with an ID, time it, and set security headers."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
    started = time.perf_counter()

    origin_token = bind_request_origin(_public_origin(request))

    try:
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
        if request.url.path.endswith(("/file", "/stream", "/poster")):
            response.headers["X-Robots-Tag"] = "noindex, noimageindex, nofollow"

        response.headers.setdefault("Cross-Origin-Resource-Policy", "cross-origin")

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
    finally:
        reset_request_origin(origin_token)


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