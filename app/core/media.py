"""
One place that decides what a media URL looks like.
"""

from contextvars import ContextVar, Token

from app.core.config import settings

_request_origin: ContextVar[str] = ContextVar("media_request_origin", default="")


def bind_request_origin(origin: str) -> Token[str]:
    """Record the origin the current request arrived on. Returns a reset token."""
    return _request_origin.set(origin.rstrip("/"))


def reset_request_origin(token: Token[str]) -> None:
    """Undo `bind_request_origin`. Always called in a `finally`."""
    _request_origin.reset(token)


def media_origin() -> str:
    """
    The origin a browser should use to fetch media right now.

    Explicit configuration first, then the live request, then nothing — which
    yields a root-relative URL and is the correct answer for a single-origin
    deployment.
    """
    return settings.public_api_origin or _request_origin.get()


def media_url(path: str) -> str:
    """Turn an API path into an address a browser can load from any origin."""
    origin = media_origin()
    if not origin:
        return path
    return f"{origin}{path if path.startswith('/') else '/' + path}"


def api_path(*segments: str, query: str = "") -> str:
    """
    Build a versioned API path from segments. Kept next to `media_url` so
    the `/api/v1` prefix is read from settings in one place rather than being
    retyped as a literal at each call site.
    """
    tail = "/".join(str(segment).strip("/") for segment in segments if str(segment))
    path = f"{settings.API_V1_PREFIX}/{tail}"
    return f"{path}?{query}" if query else path