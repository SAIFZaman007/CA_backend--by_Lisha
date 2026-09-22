"""IndexNow: tell Bing (and every IndexNow engine) the moment pages change.

Google finds pages through Search Console and the sitemap, on its own
schedule. Bing, Yandex, Seznam, Naver and Yep accept a direct push instead —
IndexNow — and new or updated URLs are typically crawled within minutes
rather than weeks. Bing's index is also what ChatGPT search and Microsoft
Copilot answer from, so this is the fastest route into AI answers.

How it is wired
---------------
* `INDEXNOW_KEY` (8–128 hex/letters/digits/dashes) is set on the API.
* The key file must be served from the *site root*:
  https://autonomyfitness.press/<key>.txt — nginx forwards `/<key>.txt` to
  `/api/v1/meta/indexnow/<key>.txt`, which answers with the key only when it
  matches (every other name is a 404).
* On each production start the API submits every public URL once, in the
  background. `python -m app.cli indexnow` does the same on demand.
"""

from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.catalog import Program

log = get_logger("seo.indexnow")

ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,128}$")

PUBLIC_PATHS = ("/", "/programs", "/about", "/gallery", "/tools", "/contact", "/privacy", "/terms")


def key_is_valid(key: str | None = None) -> bool:
    return bool(KEY_PATTERN.match(key if key is not None else settings.INDEXNOW_KEY))


async def public_urls(db: AsyncSession) -> list[str]:
    origin = settings.canonical_origin
    slugs = (
        (await db.execute(select(Program.slug).where(Program.is_active.is_(True))))
        .scalars()
        .all()
    )
    paths = [*PUBLIC_PATHS, *(f"/programs/{slug}" for slug in slugs)]
    return [f"{origin}{path}" for path in paths]


async def submit(urls: list[str]) -> tuple[bool, str]:
    """Submit up to 10,000 URLs. Returns (ok, message). Never raises."""
    if not key_is_valid():
        return False, "INDEXNOW_KEY is not set (or not a valid key)."
    if not urls:
        return False, "No URLs to submit."

    origin = settings.canonical_origin
    payload = {
        "host": origin.split("://", 1)[-1],
        "key": settings.INDEXNOW_KEY,
        "keyLocation": f"{origin}/{settings.INDEXNOW_KEY}.txt",
        "urlList": urls[:10_000],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(ENDPOINT, json=payload)
    except httpx.HTTPError as exc:
        return False, f"IndexNow unreachable: {type(exc).__name__}"

    # 200 OK / 202 Accepted = received. 403 = key file not found at keyLocation.
    if response.status_code in (200, 202):
        return True, f"{len(payload['urlList'])} URLs submitted (HTTP {response.status_code})."
    hint = {
        400: "bad request",
        403: "key file not reachable at keyLocation — check nginx and INDEXNOW_KEY",
        422: "URLs do not belong to the host, or the key does not match",
        429: "too many requests — try again later",
    }.get(response.status_code, "unexpected response")
    return False, f"IndexNow HTTP {response.status_code}: {hint}."


async def submit_all(db: AsyncSession) -> tuple[bool, str]:
    ok, message = await submit(await public_urls(db))
    (log.info if ok else log.warning)("seo.indexnow_submitted", ok=ok, detail=message)
    return ok, message