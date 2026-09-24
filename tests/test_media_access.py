"""
Private media reaches the browser that owns it.

Check-in photos, tutorial videos and message attachments are served through
signed links, because an `<img>` or `<video>` tag cannot send an Authorization
header — the browser simply requests the URL. The signature is the credential:
issued to one account, for one file, and short-lived.

This is a regression test with a story. When paid coaching was closed behind
`require_feature`, that guard ran before the endpoint and demanded a bearer
token, so every signed link in the portal answered 401 and every check-in photo
rendered as a broken image — for clients who had paid. The guard now steps
aside for a signed link and lets the endpoint verify the signature itself.
"""

import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# The smallest valid PNG: one transparent pixel.
PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
    "000049454e44ae426082"
)


@pytest.fixture(scope="session")
async def media_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
async def media_headers(media_client):
    from tests.conftest import _ensure_subscription  # noqa: PLC0415

    credentials = {
        "full_name": "Media Client",
        "email": "pytest.media@autonomyfitness.press",
        "password": "StrongPass123",
    }
    response = await media_client.post("/api/v1/auth/register", json=credentials)
    if response.status_code == 409:
        response = await media_client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": credentials["password"]},
        )
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await media_client.get("/api/v1/auth/me", headers=headers)).json()
    await _ensure_subscription(uuid.UUID(me["id"]))
    return headers


@pytest.fixture(scope="session")
async def uploaded_photo(media_client, media_headers):
    response = await media_client.post(
        "/api/v1/progress/photos",
        headers=media_headers,
        files={"file": ("front.png", io.BytesIO(PIXEL_PNG), "image/png")},
        data={"pose": "front", "note": "week 1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _path(url: str) -> str:
    """The API path and query of a signed media URL, whatever its origin."""
    return url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url.lstrip("/")


async def test_a_check_in_photo_loads_from_an_img_tag(media_client, uploaded_photo):
    """No Authorization header — exactly what a browser sends for an <img>."""
    response = await media_client.get(f"/{_path(uploaded_photo['url'])}")

    assert response.status_code == 200, (
        "a signed photo link must load without a bearer token — this is the "
        "broken-image bug in /portal/progress"
    )
    assert response.headers["content-type"].startswith("image/")
    # Private for the client's own browser, and never for a search engine.
    assert "private" in response.headers["cache-control"]
    assert "noindex" in response.headers["x-robots-tag"]


async def test_the_signature_is_what_authorises_it(media_client, uploaded_photo):
    """Strip the token and the file is refused, header or no header."""
    unsigned = f"/{_path(uploaded_photo['url'])}".split("?", 1)[0]
    response = await media_client.get(unsigned)
    assert response.status_code == 401


async def test_a_tampered_signature_is_refused(media_client, uploaded_photo):
    path = f"/{_path(uploaded_photo['url'])}"
    tampered = f"{path[:-2]}xx" if "?" in path else path
    response = await media_client.get(tampered)
    assert response.status_code == 401


async def test_the_photo_list_still_needs_a_subscription(media_client):
    """The guard is gone only for signed links, not for the routes themselves."""
    response = await media_client.get("/api/v1/progress/photos")
    assert response.status_code == 401