"""
The paywall, tested where it actually lives: the API.

A client with no live subscription may hold a perfectly valid access token —
signing up is free. These tests assert that the token alone buys nothing: every
coaching route answers 402 for them, on every method, while the account routes
they legitimately need (profile, billing, the free calculators) stay open.

If one of these starts failing, coaching has been given away.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.deps import UPGRADE_MESSAGE
from app.main import app


@pytest.fixture(scope="session")
async def paywall_client():
    """One client for the whole module — the API rate-limits sign-ins."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
async def free_headers(paywall_client):
    """A signed-in account that has never bought anything."""
    credentials = {
        "full_name": "Unpaid Client",
        "email": "pytest.unpaid@autonomyfitness.press",
        "password": "StrongPass123",
    }
    response = await paywall_client.post("/api/v1/auth/register", json=credentials)
    if response.status_code == 409:
        response = await paywall_client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": credentials["password"]},
        )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# One representative read and write per paid area. The guard is mounted on the
# router, so passing here means the whole router is closed — including routes
# added after this file was written.
PAID_ROUTES = [
    ("GET", "/api/v1/dashboard"),
    ("GET", "/api/v1/workouts/plan"),
    ("GET", "/api/v1/workouts/plans"),
    ("POST", "/api/v1/workouts/sessions"),
    ("GET", "/api/v1/exercises"),
    ("GET", "/api/v1/nutrition/plan"),
    ("POST", "/api/v1/nutrition/plan/generate"),
    ("PUT", "/api/v1/nutrition/logs"),
    ("GET", "/api/v1/progress/weight"),
    ("PUT", "/api/v1/progress/weight"),
    ("GET", "/api/v1/progress/photos"),
    ("GET", "/api/v1/wellness/sleep"),
    ("PUT", "/api/v1/wellness/sleep"),
    ("POST", "/api/v1/wellness/cardio"),
    ("GET", "/api/v1/tutorials"),
    ("GET", "/api/v1/messages/thread"),
    ("POST", "/api/v1/messages/thread"),
    ("GET", "/api/v1/messages/unread-count"),
]

# Free by design: the account itself, the way to pay, and the public tools.
FREE_ROUTES = [
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/billing/entitlement"),
    ("GET", "/api/v1/billing/summary"),
    ("GET", "/api/v1/programs"),
    ("GET", "/api/v1/calculators/reference"),
]


@pytest.mark.parametrize(("method", "path"), PAID_ROUTES)
async def test_unsubscribed_client_is_refused(paywall_client, free_headers, method, path):
    response = await paywall_client.request(method, path, headers=free_headers, json={})
    assert response.status_code == 402, f"{method} {path} let an unpaid account through"

    detail = response.json()["detail"]
    assert detail["code"] == "subscription_required"
    assert detail["message"] == UPGRADE_MESSAGE
    assert detail["upgrade_path"] == "/portal/billing"


@pytest.mark.parametrize(("method", "path"), PAID_ROUTES)
async def test_anonymous_visitor_is_refused(paywall_client, method, path):
    """No token at all still fails on authentication, never on the paywall."""
    response = await paywall_client.request(method, path, json={})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), FREE_ROUTES)
async def test_free_routes_stay_open(paywall_client, free_headers, method, path):
    response = await paywall_client.request(method, path, headers=free_headers)
    assert response.status_code < 400, f"{method} {path} is closed to a signed-in client"


async def test_entitlement_reports_no_plan(paywall_client, free_headers):
    """What the portal reads to decide which screens to unlock."""
    body = (
        await paywall_client.get("/api/v1/billing/entitlement", headers=free_headers)
    ).json()

    assert body["is_subscribed"] is False
    assert body["level"] is None
    assert body["program"] is None
    # Only the three free areas, and none of the coaching features.
    assert set(body["features"]) == {"profile", "billing", "calculators"}