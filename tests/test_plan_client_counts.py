"""
The "N clients" badge on the Pricing Plans screen.

Regression: the count used to read `WorkoutPlan.program_id`, which only the
demo seed ever wrote, so every tier showed the one demo client it was seeded
with regardless of who actually signed up. The badge now follows
`services.roster`: a live subscription first, the coach's level assignment
second, active client accounts only.

Tests share one database with the rest of the suite, so every assertion is a
delta against a count taken before this test's own rows were written.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.database import SessionLocal
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password
from app.models.billing import Subscription
from app.models.catalog import Program
from app.models.enums import SubscriptionStatus, TrainingLevel, UserRole
from app.models.user import ClientProfile, User
from app.services import roster


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    limiter.reset()
    yield


def _program(**overrides) -> Program:
    tag = uuid.uuid4().hex[:8]
    fields = {
        "slug": f"roster-{tag}",
        "name": f"Roster plan {tag}",
        "level": TrainingLevel.LEVEL_2,
        "tagline": "Fixture",
        "days_per_week": 4,
        "price_cents": 20000,
        "description": "Exists to be counted.",
        "features": ["coaching"],
    }
    fields.update(overrides)
    return Program(**fields)


def _client(level: TrainingLevel | None = None, *, active: bool = True) -> User:
    user = User(
        email=f"roster-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password=hash_password("Irrelevant123"),
        full_name="Roster Client",
        role=UserRole.CLIENT,
        is_active=active,
    )
    user.profile = ClientProfile(level=level)
    return user


async def _staff_headers(role: UserRole = UserRole.COACH) -> dict[str, str]:
    async with SessionLocal() as db:
        staff = User(
            email=f"staff-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("StaffPass123"),
            full_name="Staff Member",
            role=role,
        )
        db.add(staff)
        await db.commit()
        return {"Authorization": f"Bearer {create_access_token(staff.id, role.value)}"}


async def _counts() -> dict[uuid.UUID, int]:
    async with SessionLocal() as db:
        return await roster.program_client_counts(db)


async def test_counts_follow_subscriptions_and_level_assignments():
    async with SessionLocal() as db:
        # An archived plan sorted first must NOT own the level: the listed one
        # the public actually buys does.
        archived = _program(sort_order=-2000, is_active=False)
        current = _program(sort_order=-1000)
        db.add_all([archived, current])
        await db.flush()
        await db.commit()
        current_id, archived_id = current.id, archived.id

    before = await _counts()

    async with SessionLocal() as db:
        subscribed = _client()
        hand_assigned = _client(TrainingLevel.LEVEL_2)
        switched_off = _client(TrainingLevel.LEVEL_2, active=False)
        cancelled = _client()
        coach = User(
            email=f"coach-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("Irrelevant123"),
            full_name="Not A Client",
            role=UserRole.COACH,
        )
        coach.profile = ClientProfile(level=TrainingLevel.LEVEL_2)
        db.add_all([subscribed, hand_assigned, switched_off, cancelled, coach])
        await db.flush()

        now = datetime.now(UTC)
        db.add_all(
            [
                Subscription(
                    client_id=subscribed.id,
                    program_id=current_id,
                    status=SubscriptionStatus.ACTIVE,
                    started_at=now,
                ),
                Subscription(
                    client_id=cancelled.id,
                    program_id=current_id,
                    status=SubscriptionStatus.CANCELED,
                    started_at=now,
                ),
            ]
        )
        await db.commit()

    after = await _counts()

    # Subscribed (1) + hand-assigned Level 2 (1). Not the switched-off account,
    # not the cancelled subscription, not the staff account.
    assert after.get(current_id, 0) - before.get(current_id, 0) == 2
    assert after.get(archived_id, 0) == before.get(archived_id, 0) == 0


async def test_admin_list_reports_live_counts_and_blocks_hard_delete(client):
    async with SessionLocal() as db:
        plan = _program(level=TrainingLevel.LEVEL_3, sort_order=-5000)
        db.add(plan)
        await db.commit()
        plan_id = str(plan.id)

    headers = await _staff_headers()

    def count_for(body: list[dict]) -> int:
        return next(item["client_count"] for item in body if item["id"] == plan_id)

    response = await client.get("/api/v1/admin/programs", headers=headers)
    assert response.status_code == 200
    start = count_for(response.json())

    async with SessionLocal() as db:
        db.add_all([_client(TrainingLevel.LEVEL_3), _client(TrainingLevel.LEVEL_3)])
        await db.commit()

    response = await client.get("/api/v1/admin/programs", headers=headers)
    assert count_for(response.json()) == start + 2

    refused = await client.delete(
        f"/api/v1/admin/programs/{plan_id}", params={"hard": "true"}, headers=headers
    )
    assert refused.status_code == 409

    # Archiving (the soft path) is still always allowed.
    archived = await client.delete(f"/api/v1/admin/programs/{plan_id}", headers=headers)
    assert archived.status_code == 204