"""Test fixtures. Uses a throwaway database so tests never touch real data."""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-thirty-two-characters-long")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://coachauto:devpass@127.0.0.1:5432/coachauto_test"
)
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import Base, engine
from app.main import app


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
    """A registered client account, ready to use."""
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
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
