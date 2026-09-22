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
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    COOKIE_DOMAIN: str | None = None

    # --- Database -----------------------------------------------------------
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # --- CORS ---------------------------------------------------------------
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Public site --------------------------------------------------------
    FRONTEND_URL: str = "http://localhost:5173"

    PUBLIC_API_URL: str = ""
    SUPPORT_EMAIL: str = "coachauto2026@gmail.com"

    # --- Mail (optional; logs to stdout when unset) -------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "Coach Auto <coachauto2026@gmail.com>"
    SMTP_STARTTLS: bool = True


    EMAIL_REPLY_TO: str = ""
    EMAIL_LOGO_URL: str = ""

    # --- Media storage ------------------------------------------------------
    STORAGE_BACKEND: Literal["auto", "local", "cloudinary"] = "auto"
    CLOUDINARY_URL: str = ""
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""
    CLOUDINARY_FOLDER: str = "coach-auto"
    CLOUDINARY_AUTH_TOKEN_KEY: str = ""
    CLOUDINARY_UPLOAD_TIMEOUT: int = 600

    @field_validator(
        "CLOUDINARY_URL",
        "CLOUDINARY_CLOUD_NAME",
        "CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET",
        "CLOUDINARY_FOLDER",
        "CLOUDINARY_AUTH_TOKEN_KEY",
        mode="before",
    )
    @classmethod
    def _tidy_cloudinary(cls, value: object) -> object:
        # Values pasted from the console often carry stray spaces or quotes.
        return value.strip().strip("\"'").strip() if isinstance(value, str) else value

    @property
    def cloudinary_credentials(self) -> tuple[str, str, str] | None:
        """(cloud_name, api_key, api_secret), or None when not configured.

        Parsed here rather than handed to the SDK as `cloudinary_url=`: the
        SDK only parses a CLOUDINARY_URL it reads from the *process
        environment* at import time. Values from `.env` (which pydantic reads,
        but does not export) were stored as an inert attribute, so every upload
        failed with "Must supply api_key". The three separate variables win
        when all three are set; otherwise the URL is used.
        """
        if self.CLOUDINARY_CLOUD_NAME and self.CLOUDINARY_API_KEY and self.CLOUDINARY_API_SECRET:
            return self.CLOUDINARY_CLOUD_NAME, self.CLOUDINARY_API_KEY, self.CLOUDINARY_API_SECRET
        if not self.CLOUDINARY_URL:
            return None

        from urllib.parse import unquote, urlparse  # noqa: PLC0415

        parsed = urlparse(self.CLOUDINARY_URL)
        cloud, key, secret = parsed.hostname, parsed.username, parsed.password
        if parsed.scheme != "cloudinary" or not (cloud and key and secret):
            raise RuntimeError(
                "CLOUDINARY_URL is malformed. Expected "
                "cloudinary://<api_key>:<api_secret>@<cloud_name> — copy the "
                "'API environment variable' from the Cloudinary console."
            )
        return cloud, unquote(key), unquote(secret)

    @property
    def cloudinary_configured(self) -> bool:
        parts = (self.CLOUDINARY_CLOUD_NAME, self.CLOUDINARY_API_KEY, self.CLOUDINARY_API_SECRET)
        return bool(self.CLOUDINARY_URL or all(parts))

    @property
    def cloudinary_auth_token_key(self) -> str:
        """The token-based-auth key, only if it is plausibly one.

        This is NOT the API key: it is a separate hex key Cloudinary issues
        when token-based authentication is enabled on the account (an
        add-on). Anything that is not even-length hex would make every
        private-media URL fail to sign, so it is ignored (and logged at boot).
        """
        key = self.CLOUDINARY_AUTH_TOKEN_KEY
        if not key or len(key) % 2 or any(c not in "0123456789abcdefABCDEF" for c in key):
            return ""
        return key

    @property
    def use_cloudinary(self) -> bool:
        if self.STORAGE_BACKEND == "local":
            return False
        if self.STORAGE_BACKEND == "cloudinary":
            if not self.cloudinary_configured:
                raise RuntimeError(
                    "STORAGE_BACKEND=cloudinary but no Cloudinary credentials are set. "
                    "Provide CLOUDINARY_URL (or CLOUD_NAME / API_KEY / API_SECRET)."
                )
            return True
        return self.cloudinary_configured

    # --- Nutrition automation ----------------------------------------------
    AUTO_MEAL_PLAN_ENABLED: bool = True

    # --- Stripe -------------------------------------------------------------

    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_CURRENCY: str = "usd"

    # --- Private media ------------------------------------------------------
    MEDIA_URL_TTL_SECONDS: int = 900

    MEDIA_VIDEO_URL_TTL_SECONDS: int = 3600

    MAX_VIDEO_UPLOAD_MB: int = 512
    ALLOWED_VIDEO_TYPES: Annotated[list[str], NoDecode] = [
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]

    UPLOAD_DIR: str = "/app/uploads"
    MAX_UPLOAD_MB: int = 8
    ALLOWED_IMAGE_TYPES: Annotated[list[str], NoDecode] = ["image/jpeg", "image/png", "image/webp"]

    # --- Message attachments ------------------------------------------------
    MAX_MESSAGE_IMAGE_MB: int = 6
    MAX_ATTACHMENTS_PER_MESSAGE: int = 6
    ORPHAN_ATTACHMENT_TTL_HOURS: int = 24

    @field_validator("ALLOWED_IMAGE_TYPES", "ALLOWED_VIDEO_TYPES", mode="before")
    @classmethod
    def _split_types(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    # --- SEO ----------------------------------------------------------------
    # The ONE public domain. Sitemap, robots.txt and every canonical URL use
    # it; it must equal VITE_SITE_URL on the frontend. A sitemap listing a
    # different domain than the one Google crawls is ignored outright.
    CANONICAL_SITE_URL: str = "https://autonomyfitness.press"
    SEO_DEFAULT_IMAGE: str = "/images/og-cover.jpg"
    SEO_LOCALE: str = "en_US"
    BUSINESS_REGION: str = "US"

    @property
    def canonical_origin(self) -> str:
        """
        CANONICAL_SITE_URL with any trailing slash removed.

        Every caller concatenates a path onto this. One stray slash in the env
        file otherwise produces `https://site.com//programs` throughout the
        sitemap — which Google treats as a separate URL from the real one and
        then reports as duplicate content.
        """
        return self.CANONICAL_SITE_URL.rstrip("/")

    # --- Rate limiting ------------------------------------------------------
    RATE_LIMIT_DEFAULT: str = "200/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_PUBLIC_FORM: str = "5/minute"

    # --- Seeding ------------------------------------------------------------
    SEED_ON_STARTUP: bool = False
    COACH_EMAIL: str = "lisha.chesson@coach-auto.org"
    COACH_PASSWORD: str | None = None
    SEED_DEMO_CLIENTS: bool = True

    @property
    def public_api_origin(self) -> str:
        """
        PUBLIC_API_URL with any trailing slash removed, or "" when unset.

        Callers concatenate a path onto this, so a stray slash in the env file
        would otherwise produce `https://api.example.com//api/v1/...`.
        """
        return self.PUBLIC_API_URL.rstrip("/")

    @property
    def email_reply_to(self) -> str:
        """Where replies to a transactional email should go."""
        return self.EMAIL_REPLY_TO or self.SUPPORT_EMAIL

    @property
    def email_logo_url(self) -> str:
        """Absolute URL of the brand logo for the email header.

        Derived from FRONTEND_URL so a staging deployment does not hotlink the
        production site, and so this is one setting rather than two.
        """
        if self.EMAIL_LOGO_URL:
            return self.EMAIL_LOGO_URL
        return f"{self.FRONTEND_URL.rstrip('/')}/images/logo-lockup-light.png"

    @property
    def trusted_hosts(self) -> list[str]:
        """
        Hostnames this API will answer to in production.

        Derived from the origins already configured rather than hard-coded, so
        a new deployment domain is one environment variable and not a code
        change. `TrustedHostMiddleware` matches on hostname only, so the
        scheme and any port are stripped here.
        """
        origins = [*self.CORS_ORIGINS, self.PUBLIC_API_URL, self.FRONTEND_URL]
        hosts: list[str] = ["localhost", "127.0.0.1"]
        for origin in origins:
            if not origin:
                continue
            host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def db_host(self) -> str:
        """
        The bare hostname of DATABASE_URL — safe to print in a log line or
        a CLI confirmation prompt, unlike the DSN itself, which carries the
        password in plaintext.

        `PostgresDsn` is a `MultiHostUrl` in Pydantic v2 (Postgres connection
        strings can legally name more than one host, for replica failover), so
        it exposes `.hosts()` — a list — rather than a single `.host`
        attribute. There is no `.host` on this type; reading it raises
        `AttributeError`, which is exactly what took the CLI down here. This
        reads the first entry from the list, which is the only host in the
        overwhelming majority of setups, including this one.
        """
        hosts = self.DATABASE_URL.hosts()
        if hosts and hosts[0].get("host"):
            return hosts[0]["host"]
        return "unknown-host"

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