"""
Changing a password while signed in — the coach dashboard's Account settings
and the client portal's profile screen share this endpoint.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import SessionLocal
from app.core.rate_limit import limiter
from app.core.security import hash_password
from app.main import app
from app.models.enums import UserRole
from app.models.user import User

OLD = "CoachPass123"
NEW = "NewCoachPass456"


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    limiter.reset()
    yield


async def _make_coach() -> str:
    email = f"pw-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionLocal() as db:
        db.add(
            User(
                email=email,
                hashed_password=hash_password(OLD),
                full_name="Password Coach",
                role=UserRole.COACH,
            )
        )
        await db.commit()
    return email


def _browser() -> AsyncClient:
    """A separate cookie jar — one device."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _sign_in(browser: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await browser.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_rejects_wrong_current_same_and_weak_passwords():
    email = await _make_coach()
    async with _browser() as browser:
        headers = await _sign_in(browser, email, OLD)

        wrong = await browser.post(
            "/api/v1/auth/change-password",
            json={"current_password": "NotIt12345", "new_password": NEW},
            headers=headers,
        )
        assert wrong.status_code == 400

        same = await browser.post(
            "/api/v1/auth/change-password",
            json={"current_password": OLD, "new_password": OLD},
            headers=headers,
        )
        assert same.status_code == 400
        assert "different" in same.json()["detail"]

        weak = await browser.post(
            "/api/v1/auth/change-password",
            json={"current_password": OLD, "new_password": "short"},
            headers=headers,
        )
        assert weak.status_code == 422

        anonymous = await browser.post(
            "/api/v1/auth/change-password",
            json={"current_password": OLD, "new_password": NEW},
        )
        assert anonymous.status_code == 401


async def test_change_keeps_this_session_and_signs_out_other_devices():
    email = await _make_coach()
    async with _browser() as laptop, _browser() as phone:
        laptop_headers = await _sign_in(laptop, email, OLD)
        await _sign_in(phone, email, OLD)

        changed = await laptop.post(
            "/api/v1/auth/change-password",
            json={"current_password": OLD, "new_password": NEW},
            headers=laptop_headers,
        )
        assert changed.status_code == 204

        # The device that made the change stays signed in...
        assert (await laptop.post("/api/v1/auth/refresh?audience=staff")).status_code == 200
        # ...every other device is signed out.
        assert (await phone.post("/api/v1/auth/refresh?audience=staff")).status_code == 401

    async with _browser() as fresh:
        old_login = await fresh.post(
            "/api/v1/auth/login", json={"email": email, "password": OLD}
        )
        assert old_login.status_code == 401
        await _sign_in(fresh, email, NEW)