"""
The intake form and the training block it produces.

Two promises are tested here, because they are the ones a client notices:

* Fill the form in and a plan exists immediately — with movements they can
  actually perform, in the place they actually train.
* A plan the coach wrote is never replaced by a generated one.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models.catalog import Exercise
from app.models.enums import Equipment, Mechanics, MuscleGroup, TrainingLevel
from app.models.training import WorkoutPlan
from app.services.workout_planner import SOURCE_AUTO, SOURCE_MANUAL

# A miniature exercise library: two ways to train each pattern, one needing a
# gym and one needing nothing, so "did the equipment filter work?" has a real
# answer rather than an empty plan.
LIBRARY = [
    ("Barbell Back Squat", MuscleGroup.QUADS, Equipment.BARBELL, Mechanics.COMPOUND),
    ("Bodyweight Squat", MuscleGroup.QUADS, Equipment.BODYWEIGHT, Mechanics.COMPOUND),
    ("Barbell Bench Press", MuscleGroup.CHEST, Equipment.BARBELL, Mechanics.COMPOUND),
    ("Push-Up", MuscleGroup.CHEST, Equipment.BODYWEIGHT, Mechanics.COMPOUND),
    ("Cable Row", MuscleGroup.UPPER_BACK, Equipment.CABLE, Mechanics.COMPOUND),
    ("Band Row", MuscleGroup.UPPER_BACK, Equipment.BAND, Mechanics.COMPOUND),
    ("Lat Pulldown", MuscleGroup.LATS, Equipment.CABLE, Mechanics.COMPOUND),
    ("Band Pullover", MuscleGroup.LATS, Equipment.BAND, Mechanics.ISOLATION),
    ("Romanian Deadlift", MuscleGroup.HAMSTRINGS, Equipment.BARBELL, Mechanics.COMPOUND),
    ("Glute Bridge", MuscleGroup.GLUTES, Equipment.BODYWEIGHT, Mechanics.COMPOUND),
    ("Dumbbell Shoulder Press", MuscleGroup.SHOULDERS, Equipment.DUMBBELL, Mechanics.COMPOUND),
    ("Pike Push-Up", MuscleGroup.SHOULDERS, Equipment.BODYWEIGHT, Mechanics.COMPOUND),
    ("Dumbbell Curl", MuscleGroup.BICEPS, Equipment.DUMBBELL, Mechanics.ISOLATION),
    ("Band Curl", MuscleGroup.BICEPS, Equipment.BAND, Mechanics.ISOLATION),
    ("Triceps Pushdown", MuscleGroup.TRICEPS, Equipment.CABLE, Mechanics.ISOLATION),
    ("Bench Dip", MuscleGroup.TRICEPS, Equipment.BODYWEIGHT, Mechanics.ISOLATION),
    ("Plank", MuscleGroup.ABS, Equipment.BODYWEIGHT, Mechanics.ISOLATION),
    ("Side Plank", MuscleGroup.OBLIQUES, Equipment.BODYWEIGHT, Mechanics.ISOLATION),
    ("Standing Calf Raise", MuscleGroup.CALVES, Equipment.BODYWEIGHT, Mechanics.ISOLATION),
]

GYM_INTAKE = {
    "height_cm": 178,
    "current_weight_kg": 84,
    "goal": "build",
    "training_location": "gym",
    "training_experience": "intermediate",
    "available_equipment": [],
    "days_per_week": 4,
    "session_minutes": 60,
}

HOME_INTAKE = {
    **GYM_INTAKE,
    "goal": "cut",
    "training_location": "home",
    "training_experience": "beginner",
    "available_equipment": ["bodyweight", "band"],
    "days_per_week": 3,
    "session_minutes": 30,
}


@pytest.fixture(scope="session", autouse=True)
async def _library():
    """Seed the movements, each with a demonstration video (a plan rule)."""
    async with SessionLocal() as db:
        existing = (await db.execute(select(Exercise.slug))).scalars().all()
        for name, group, equipment, mechanics in LIBRARY:
            slug = name.lower().replace(" ", "-").replace("/", "-")
            if slug in existing:
                continue
            db.add(
                Exercise(
                    slug=slug,
                    name=name,
                    muscle_group=group,
                    target_muscle=group.value.replace("_", " ").title(),
                    equipment=equipment,
                    mechanics=mechanics,
                    video_url=f"https://videos.example.com/{slug}.mp4",
                    min_level=TrainingLevel.LEVEL_1,
                    is_active=True,
                )
            )
        await db.commit()


@pytest.fixture(scope="session")
async def intake_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
async def client_headers(intake_client):
    """A subscribed client — coaching routes are paid."""
    from tests.conftest import _ensure_subscription  # noqa: PLC0415

    credentials = {
        "full_name": "Intake Client",
        "email": "pytest.intake@autonomyfitness.press",
        "password": "StrongPass123",
    }
    response = await intake_client.post("/api/v1/auth/register", json=credentials)
    if response.status_code == 409:
        response = await intake_client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": credentials["password"]},
        )
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await intake_client.get("/api/v1/auth/me", headers=headers)).json()
    await _ensure_subscription(uuid.UUID(me["id"]))
    return headers


async def test_intake_starts_empty_and_offers_equipment(intake_client, client_headers):
    body = (await intake_client.get("/api/v1/workouts/intake", headers=client_headers)).json()

    assert body["intake"]["is_complete"] is False
    assert body["has_plan"] is False
    labels = {option["value"] for option in body["equipment_options"]}
    assert {"bodyweight", "dumbbell", "barbell", "band"} <= labels


async def test_no_plan_before_the_intake(intake_client, client_headers):
    response = await intake_client.get("/api/v1/workouts/plan", headers=client_headers)
    assert response.status_code == 200
    assert response.json() is None


async def test_filling_the_intake_produces_a_plan(intake_client, client_headers):
    response = await intake_client.put(
        "/api/v1/workouts/intake", headers=client_headers, json=GYM_INTAKE
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["intake"]["is_complete"] is True
    plan = body["plan"]
    assert plan is not None, body.get("message")

    # A four-day week is an upper/lower split, and every day has movements.
    assert len(plan["days"]) == 4
    assert all(day["exercises"] for day in plan["days"])
    assert plan["source"] == SOURCE_AUTO

    # "Build" means heavier compounds with full rests.
    opener = plan["days"][0]["exercises"][0]
    assert opener["rep_range"] == "6-10"
    assert opener["rest_seconds"] == 120

    # The rule borrowed from the coach's plan builder: nothing without a video.
    for day in plan["days"]:
        for item in day["exercises"]:
            assert item["exercise"]["video_url"]

    # And the plan the client trains from is now this one.
    active = (await intake_client.get("/api/v1/workouts/plan", headers=client_headers)).json()
    assert active["id"] == plan["id"]


async def test_home_intake_only_prescribes_what_the_client_owns(intake_client, client_headers):
    """The equipment answer is a hard filter — this is the one that matters."""
    response = await intake_client.put(
        "/api/v1/workouts/intake", headers=client_headers, json=HOME_INTAKE
    )
    plan = response.json()["plan"]
    assert plan is not None

    owned = {"bodyweight", "band", "stretch", "foam_roller"}
    prescribed = {
        item["exercise"]["equipment"] for day in plan["days"] for item in day["exercises"]
    }
    assert prescribed <= owned, f"prescribed unavailable equipment: {prescribed - owned}"

    # Three days for a beginner is full body, and a 30-minute session carries
    # fewer movements than an hour.
    assert len(plan["days"]) == 3
    assert all(len(day["exercises"]) <= 3 for day in plan["days"])

    # "Cut" shortens the rests.
    assert plan["days"][0]["exercises"][0]["rest_seconds"] == 60


async def test_regenerating_keeps_one_active_plan(intake_client, client_headers):
    before = (await intake_client.get("/api/v1/workouts/plans", headers=client_headers)).json()
    response = await intake_client.post("/api/v1/workouts/plan/generate", headers=client_headers)
    assert response.status_code == 200

    plans = (await intake_client.get("/api/v1/workouts/plans", headers=client_headers)).json()
    assert len(plans) == len(before) + 1
    assert sum(1 for plan in plans if plan["id"] == response.json()["plan"]["id"]) == 1

    active = (await intake_client.get("/api/v1/workouts/plan", headers=client_headers)).json()
    assert active["id"] == response.json()["plan"]["id"]


async def test_a_coach_written_plan_is_never_replaced(intake_client, client_headers):
    me = (await intake_client.get("/api/v1/auth/me", headers=client_headers)).json()

    async with SessionLocal() as db:
        db.add(
            WorkoutPlan(
                client_id=uuid.UUID(me["id"]),
                name="Coach block — peak week",
                level=TrainingLevel.LEVEL_1,
                is_active=True,
                source=SOURCE_MANUAL,
            )
        )
        # Everything else steps aside, exactly as the coach's own endpoint does.
        for plan in (
            await db.execute(
                select(WorkoutPlan).where(WorkoutPlan.client_id == uuid.UUID(me["id"]))
            )
        ).scalars():
            if plan.name != "Coach block — peak week":
                plan.is_active = False
        await db.commit()

    response = await intake_client.post("/api/v1/workouts/plan/generate", headers=client_headers)
    body = response.json()

    assert response.status_code == 200
    assert body["plan"]["name"] == "Coach block — peak week"
    assert "coach" in body["message"].lower()