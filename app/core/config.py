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

    # The origin a *browser* uses to reach this API. Leave blank when the SPAs
    # are served from the same origin as the API (local Vite proxy, or an
    # nginx that proxies /api/ to the backend). Set it — e.g.
    # "https://ca-backend.maktechgroups.com" — whenever the SPAs call the API
    # on a different hostname.
    #
    # This is what makes private media work. Every signed media URL the API
    # mints (message attachments, check-in photos, tutorial streams) is
    # embedded in an <img>/<video> `src`, and a browser resolves a root-relative
    # `src` against the *page* origin, not against whatever origin the SPA's
    # XHR client is configured with. Emit a relative path while the SPA lives
    # on a different host and every image and video request lands on the
    # frontend's own web server instead of the API. Building these URLs from
    # this setting removes the ambiguity: the API states its own address.
    PUBLIC_API_URL: str = ""
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
    # --- Stripe -------------------------------------------------------------
    # Leave blank in development: `stripe_gateway.is_configured()` returns
    # False and the app boots normally, it just refuses to open a checkout.
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_CURRENCY: str = "usd"

    # --- Private media ------------------------------------------------------
    # A browser <img> tag cannot send an Authorization header, so private
    # images are addressed with a short-lived signed URL instead. Long enough
    # to load a gallery, short enough that a copied link is useless by the time
    # it is pasted anywhere.
    MEDIA_URL_TTL_SECONDS: int = 900

    # A <video> tag has the same "cannot send a header" problem as <img>, but a
    # very different usage pattern: a photo loads once and is done, while a
    # video keeps issuing new range requests for as long as someone is
    # watching, scrubbing, or has it paused on a tab they haven't closed. A
    # signed URL embedded once in `src` does not get re-minted just because
    # playback is still going, so it has to outlive a realistic viewing
    # session on its own — a coaching demo clip, paused and resumed a few
    # times, comfortably fits in an hour. `stream_tutorial` also accepts a
    # normal Bearer access token as a first-class alternative (see
    # `OptionalUser` there), so a client that *can* send one — the dashboard's
    # own preview player, for instance — is never limited by this at all; this
    # TTL only bounds the signed-URL fallback that a bare <video> tag needs.
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
    # Deliberately smaller than the general image limit. These come off a phone
    # camera on gym wi-fi, are downscaled to 1600px on arrival anyway, and a
    # client watching a 12 MB upload crawl is a client who stops sending
    # progress photos. Six megabytes is more than a phone JPEG needs.
    MAX_MESSAGE_IMAGE_MB: int = 6
    MAX_ATTACHMENTS_PER_MESSAGE: int = 6
    # An attachment uploaded but never attached to a message is rubbish on the
    # volume. Anything unbound and older than this is swept.
    ORPHAN_ATTACHMENT_TTL_HOURS: int = 24

    @field_validator("ALLOWED_IMAGE_TYPES", "ALLOWED_VIDEO_TYPES", mode="before")
    @classmethod
    def _split_types(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    # --- SEO ----------------------------------------------------------------
    # The one canonical origin. Every absolute URL the API emits — sitemap
    # entries, JSON-LD `@id` values, Open Graph images — is built from this, so
    # a staging deployment cannot leak `localhost` into a schema block that
    # Google then indexes. Kept separate from FRONTEND_URL, which is where the
    # API sends people (password reset links, Stripe returns) and may legitimately
    # differ in a preview environment.
    CANONICAL_SITE_URL: str = "https://autonomyfitness.press"
    SEO_DEFAULT_IMAGE: str = "/images/hero-portrait.png"
    SEO_LOCALE: str = "en_US"
    # Emitted in the sitemap and in `<meta name="geo.region">`. The business is
    # online-only, so this is where the coach operates from, not a shopfront.
    BUSINESS_REGION: str = "US"

    @property
    def canonical_origin(self) -> str:
        """CANONICAL_SITE_URL with any trailing slash removed.

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
    # The one real credential seeding can create. Read by app/services/seed.py
    # — never hardcode this anywhere else. Required to seed the coach account
    # in production; outside production a dev-only fallback is used instead
    # if this is left blank, so a fresh clone still has a working login.
    COACH_PASSWORD: str | None = None

    # Seeded everywhere by default, including production — a handful of
    # clearly fake @example.com accounts alongside real customers has never
    # been the problem. Set to false to opt out, e.g. on a database you want
    # to keep completely clean.
    SEED_DEMO_CLIENTS: bool = True

    @property
    def public_api_origin(self) -> str:
        """PUBLIC_API_URL with any trailing slash removed, or "" when unset.

        Callers concatenate a path onto this, so a stray slash in the env file
        would otherwise produce `https://api.example.com//api/v1/...`.
        """
        return self.PUBLIC_API_URL.rstrip("/")

    @property
    def trusted_hosts(self) -> list[str]:
        """Hostnames this API will answer to in production.

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
        """The bare hostname of DATABASE_URL — safe to print in a log line or
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