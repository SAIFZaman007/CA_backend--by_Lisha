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

The fix is for the API to state its own address instead of assuming the caller
shares it. `media_url` is the single choke point where that happens: pass it an
API path, get back something a browser can load from anywhere.
"""

from app.core.config import settings


def media_url(path: str) -> str:
    """
    Turn an API path into an address a browser can load from any origin.

    Absolute when `PUBLIC_API_URL` is configured, unchanged when it is not —
    so a same-origin deployment (the Vite dev proxy, or an nginx that proxies
    `/api/` to the backend) keeps working with no configuration at all, and a
    split-origin deployment is one environment variable away from correct.
    """
    origin = settings.public_api_origin
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