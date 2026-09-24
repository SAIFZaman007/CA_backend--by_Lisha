"""Test fixtures. Uses a throwaway database so tests never touch real data."""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-thirty-two-characters-long")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://coachauto:devpass@127.0.0.1:5432/coachauto_test"
)
os.environ.setdefault("COOKIE_SECURE", "false")
# Media goes to a throwaway directory, never to the real Cloudinary account:
# a test run must not upload anything, and must not need network access.
# (Cleared rather than forcing STORAGE_BACKEND=local, so the storage-config
# tests can still exercise the "auto" decision they were written for.)
for _cloudinary_var in (
    "CLOUDINARY_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
):
    os.environ.setdefault(_cloudinary_var, "")
os.environ.setdefault("UPLOAD_DIR", "/tmp/coachauto-test-uploads")

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.billing import Subscription
from app.models.catalog import Program
from app.models.enums import ENTITLING_STATUSES, SubscriptionStatus, TrainingLevel


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def auth_headers(client):
    """
    A registered client account **with an active plan**, ready to use.

    Coaching routes are paid (`core.deps.require_feature`), so a bare sign-up
    can only ever see 402 — which is what most tests would then be asserting
    about, instead of the behaviour they were written for. This fixture is the
    ordinary case: someone who has bought coaching. The unpaid case has tests
    of its own in `test_entitlement_guard.py`.
    """
    payload = {
        "full_name": "Test Client",
        "email": "pytest.client@autonomyfitness.press",
        "password": "StrongPass123",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    if response.status_code == 409:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": payload["email"], "password": payload["password"]},
        )
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    await _ensure_subscription(uuid.UUID(me["id"]))
    return headers


async def _ensure_subscription(client_id: uuid.UUID) -> None:
    """Give `client_id` a live Level 1 subscription, once."""
    async with SessionLocal() as db:
        program = (
            await db.execute(select(Program).where(Program.slug == "pytest-level-1"))
        ).scalars().first()
        if program is None:
            program = Program(
                slug="pytest-level-1",
                name="Pytest Level 1",
                level=TrainingLevel.LEVEL_1,
                tagline="Fixture plan",
                days_per_week=3,
                price_cents=9900,
                description="A plan that exists so paid routes can be tested.",
                features=["coaching"],
            )
            db.add(program)
            await db.flush()

        existing = (
            await db.execute(
                select(Subscription).where(
                    Subscription.client_id == client_id,
                    Subscription.status.in_(ENTITLING_STATUSES),
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(
                Subscription(
                    client_id=client_id,
                    program_id=program.id,
                    status=SubscriptionStatus.ACTIVE,
                    price_cents=program.price_cents,
                    started_at=datetime.now(UTC),
                )
            )
        await db.commit()