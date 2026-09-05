"""
One place that decides what a media URL looks like.

Every file this API serves — message attachments, check-in photos, gallery
images, programme artwork, uploaded tutorial videos — is addressed through a
route on this API, and every one of those addresses ends up inside an `<img>`
or `<video>` `src` in a browser.

That is the whole reason this module exists. A browser resolves a root-relative
`src` against the *page's* origin. It does not know, and cannot be told, that
the SPA's XHR client is pointed at a different hostname. So the moment the
portal is served from one domain and the API from another, a relative
`/api/v1/...` media path stops meaning "the API" and starts meaning "whatever
web server happens to be answering for the portal" — which is not this
application, and which answers with a 404 or a 502.

--- Why the origin is derived from the request ---------------------------------

The first version of this module read a single `PUBLIC_API_URL` setting. That
works for one SPA. It does not work for two.

This platform serves two front ends from two different hostnames — the client
portal and the coach dashboard — against one API. A single hard-coded media
origin is necessarily wrong for at least one of them the moment they do not
reach the API the same way, and "wrong" here is silent: a cross-origin image
that is not in the page's `img-src` allowlist is refused by the browser before
a request is ever made, and an `<img>` reports that as an ordinary load error.
Nothing appears in the API log, because nothing reached the API.

So the origin is taken from the request being answered. Whatever hostname a
browser used to reach this API is, by definition, a hostname that browser can
reach — and it is already inside that page's connect-src, because the JSON call
carrying this very response went to it. Same-origin deployments get relative
behaviour for free (the derived origin matches the page), split-origin
deployments get an absolute URL that points at the right place, and neither
needs a setting.

`PUBLIC_API_URL` still wins when set. Some URLs are minted outside a request —
an email, a sitemap, a CLI task — and those need a fixed public address.
"""

from contextvars import ContextVar, Token

from app.core.config import settings

# Bound per request by the middleware in `app.main`. A ContextVar rather than a
# parameter threaded through every serialiser: the alternative is passing a
# Request object into a dozen call sites across five endpoint modules, all of
# which only want a string. Task-local, so concurrent requests on the same
# worker never see each other's value.
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