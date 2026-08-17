"""Application settings. Everything configurable lives here, nothing is hard-coded."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Identity -----------------------------------------------------------
    PROJECT_NAME: str = "Coach Auto API"
    BRAND_NAME: str = "Coach Auto"
    BUSINESS_NAME: str = "Autonomy Health and Fitness"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # --- Security -----------------------------------------------------------
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30
    # Refresh tokens are stored in an HttpOnly cookie; set False only for local HTTP.
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    COOKIE_DOMAIN: str | None = None

    # --- Database -----------------------------------------------------------
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # --- CORS ---------------------------------------------------------------
    # Comma-separated list, e.g. "https://autonomyfitness.press,https://www.autonomyfitness.press"
    # NoDecode stops pydantic-settings trying to JSON-parse the env value first,
    # so the plain comma-separated form below is what actually gets used.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Public site --------------------------------------------------------
    FRONTEND_URL: str = "http://localhost:5173"
    SUPPORT_EMAIL: str = "coachauto2026@gmail.com"
    INSTAGRAM_URL: str = "https://www.instagram.com/coach.auto"

    # --- Mail (optional; logs to stdout when unset) -------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "Coach Auto <coachauto2026@gmail.com>"
    SMTP_STARTTLS: bool = True

    # --- Uploads ------------------------------------------------------------
    UPLOAD_DIR: str = "/app/uploads"
    MAX_UPLOAD_MB: int = 8
    ALLOWED_IMAGE_TYPES: Annotated[list[str], NoDecode] = ["image/jpeg", "image/png", "image/webp"]

    @field_validator("ALLOWED_IMAGE_TYPES", mode="before")
    @classmethod
    def _split_types(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    # --- Rate limiting ------------------------------------------------------
    RATE_LIMIT_DEFAULT: str = "200/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_PUBLIC_FORM: str = "5/minute"

    # --- Seeding ------------------------------------------------------------
    SEED_ON_STARTUP: bool = False
    COACH_EMAIL: str = "coachauto2026@gmail.com"
    COACH_PASSWORD: str | None = None  # required only when SEED_ON_STARTUP is true

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def sqlalchemy_url(self) -> str:
        """Force the asyncpg driver regardless of how the URL was supplied."""
        url = str(self.DATABASE_URL)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
