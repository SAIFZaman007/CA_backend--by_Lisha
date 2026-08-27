"""The one and only seed.

Run with `python -m app.cli seed`. No prompts, no confirmation, no
environment check — it just runs, every time, everywhere. Every insert is
guarded by its own existence check, so running it against a database that
already has some of this data fills in whatever is missing rather than
skipping the whole thing or duplicating what's there.

What it creates:

  * the three coaching tiers (Level 1, 2 and 3) with their public copy
  * the exercise library and the published testimonials
  * ONE staff account — the coach, who is also the super admin
  * demo client accounts covering the states worth testing: a brand-new
    sign-up with no plan, and one subscribed client per tier — each with a
    real assigned training block, a meal plan, weeks of sleep/cardio/weight
    history, body measurements, a check-in photo, and a live message thread
  * a handful of website enquiries and consultation bookings, so the coach
    dashboard's pipeline screens have real, distinct, realistic content

A note on why every piece is checked individually rather than the whole
client being skipped if the email already exists: the previous version
gated everything on one `User` existence check, which meant re-running the
seed against an already-seeded database (exactly what "add richer demo
data" needs to do) did nothing at all for accounts that already existed.
Every sub-piece below — the plan, the meal plan, the sleep history, the
subscription — now checks for itself, so this script is safe and useful to
run again at any time, on a fresh database or a partially-seeded one.

A note on levels. A client's level comes from a paid subscription, not from
a column somebody set by hand. Seeded clients get a real `Subscription` row
apiece; the profile level is derived from it through the same `entitlements`
code the webhook uses, so seeded data and production data travel identical
paths. The brand-new sign-up has no subscription and no level, which is
exactly what the portal should treat as "choose a plan" — left alone on
purpose, as the one deliberately empty state worth keeping in the demo.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.services.exercise_import import sync_catalog
from app.models.billing import Subscription
from app.models.catalog import Exercise, Program, Testimonial
from app.models.engagement import ConsultationBooking, Lead, Message, MessageThread
from app.models.enums import (
    ActivityLevel,
    BookingStatus,
    CardioType,
    DataSource,
    Equipment,
    Goal,
    Intensity,
    LeadStatus,
    PhotoPose,
    Sex,
    SessionStatus,
    SubscriptionStatus,
    TrainingLevel,
    UnitSystem,
    UserRole,
)
from app.models.nutrition import Meal, MealItem, MealPlan
from app.models.tracking import BodyMeasurement, CardioLog, ProgressPhoto, SleepLog, WeightLog
from app.models.training import WorkoutDay, WorkoutDayExercise, WorkoutPlan, WorkoutSession, SetLog
from app.models.user import ClientProfile, User
from app.services import entitlements

log = get_logger("seed")

COACH_DISPLAY_NAME = "Lisha Chesson"
DEV_FALLBACK_COACH_PASSWORD = "DevCoach!2026"

PROGRAMS = [
    {
        "slug": "level-1-strength-foundation",
        "name": "Level 1 — Strength Foundation",
        "level": TrainingLevel.LEVEL_1,
        "tagline": "Three days a week. Build the base the right way.",
        "days_per_week": 3,
        "session_minutes": 55,
        "price_cents": 9900,
        "best_for": "New to lifting, or coming back after a long break",
        "description": (
            "A three-day full-body split that teaches you the six movement patterns before it "
            "asks you to load them. Every session is written for you, every exercise comes with "
            "a video, and your coach reviews your logs each week."
        ),
        "features": [
            "3 training days per week",
            "Full exercise video library",
            "Personalised meal plan and macro targets",
            "Weekly check-in with measurements and photos",
            "Sleep and cardio tracking",
            "Direct message access to Coach Auto",
        ],
    },
    {
        "slug": "level-2-strength-builder",
        "name": "Level 2 — Strength Builder",
        "level": TrainingLevel.LEVEL_2,
        "tagline": "Four days a week. Add volume, add muscle.",
        "days_per_week": 4,
        "session_minutes": 65,
        "price_cents": 9900,
        "best_for": "Six months or more of consistent training",
        "description": (
            "An upper/lower split across four days with progressive overload built into the "
            "programming. You move up to Level 2 when your coach assesses that your technique "
            "and recovery can carry the extra work."
        ),
        "features": [
            "4 training days per week",
            "Upper/lower split with planned progression",
            "Adjusted macros for a build or cut phase",
            "Bi-weekly programme review",
            "Sleep, cardio and recovery tracking",
            "Priority message replies",
        ],
    },
    {
        "slug": "level-3-competition-prep",
        "name": "Level 3 — Advanced Athlete",
        "level": TrainingLevel.LEVEL_3,
        "tagline": "Five to six days a week. For athletes who already know the work.",
        "days_per_week": 5,
        "session_minutes": 75,
        "price_cents": 9900,
        "best_for": "Advanced lifters and stage prep",
        "description": (
            "A five to six day body-part split with periodised intensity, peak weeks and "
            "conditioning prescribed around your training. Places are limited and taken by "
            "assessment only."
        ),
        "features": [
            "5–6 training days per week",
            "Periodised blocks with deload weeks",
            "Peak-week and stage-prep guidance",
            "Weekly video form review",
            "Full sleep, cardio and recovery analysis",
            "Same-day message replies",
        ],
    },
]

# The exercise library itself no longer lives here — see app.data.exercise_library
# for the full 214-movement catalogue (all 22 muscle groups, every equipment
# type) and app.services.exercise_import.sync_catalog for how it's loaded.
# WORKOUT_DAY_TEMPLATES below references movements by name; every name it uses
# exists in that catalogue with identical spelling, since the templates were
# written against it directly.

TESTIMONIALS = [
    ("Sandra T.", "Level 1 Client", 5, 8,
     "I had never touched a barbell. Eight weeks in I squat with a straight back and I know "
     "why every set is in my programme. The videos meant I never had to guess.", "-4.8 lbs"),
    ("Marcus R.", "Level 2 Client", 5, 16,
     "The check-ins are what changed it for me. Weight, tape, photos, sleep — all in one place, "
     "and Coach reads it every week and adjusts. Nothing gets away from you.", "+11 lbs lean"),
    ("Priya K.", "Level 1 Client", 5, 12,
     "I work shifts and my sleep was wrecked. Logging it showed the pattern, we moved my "
     "training days, and my lifts finally started moving again.", "-3 in waist"),
]

# --- Demo client accounts -------------------------------------------------------
CLIENTS = [
    {
        "email": "sandra.thompson@example.com",
        "password": "Client!2345",
        "full_name": "Sandra Thompson",
        "display_name": "Sandra T.",
        "sex": Sex.FEMALE,
        "tier": "level-1-strength-foundation",
        "goal": Goal.CUT,
        "height_cm": 168,
        "start_kg": 72.5,
        "current_kg": 69.9,
        "goal_kg": 66.0,
        "weeks_in": 8,
        "calorie_target": 1800,
        "protein_target_g": 150,
        "carb_target_g": 165,
        "fat_target_g": 55,
    },
    {
        "email": "marcus.reed@example.com",
        "password": "Client!2345",
        "full_name": "Marcus Reed",
        "display_name": "Marcus R.",
        "sex": Sex.MALE,
        "tier": "level-2-strength-builder",
        "goal": Goal.BUILD,
        "height_cm": 180,
        "start_kg": 78.0,
        "current_kg": 81.2,
        "goal_kg": 86.0,
        "weeks_in": 14,
        "calorie_target": 2900,
        "protein_target_g": 190,
        "carb_target_g": 330,
        "fat_target_g": 80,
    },
    {
        "email": "priya.kapoor@example.com",
        "password": "Client!2345",
        "full_name": "Priya Kapoor",
        "display_name": "Priya K.",
        "sex": Sex.FEMALE,
        "tier": "level-3-competition-prep",
        "goal": Goal.MAINTAIN,
        "height_cm": 165,
        "start_kg": 61.0,
        "current_kg": 60.4,
        "goal_kg": 60.0,
        "weeks_in": 26,
        "calorie_target": 2000,
        "protein_target_g": 130,
        "carb_target_g": 220,
        "fat_target_g": 65,
    },
    {
        "email": "new.signup@example.com",
        "password": "Client!2345",
        "full_name": "Jordan Ellis",
        "display_name": "Jordan E.",
        "sex": None,
        "tier": None,
        "goal": Goal.CUT,
        "height_cm": 174,
        "start_kg": None,
        "current_kg": None,
        "goal_kg": None,
        "weeks_in": 0,
        "calorie_target": None,
        "protein_target_g": None,
        "carb_target_g": None,
        "fat_target_g": None,
    },
]

# --- Training templates ----------------------------------------------------
WORKOUT_DAY_TEMPLATES = [
    {
        "focus": "Lower Body",
        "exercises": [
            ("Barbell Back Squat", 4, "6-8", 120, 7),
            ("Romanian Deadlift", 3, "8-10", 90, 9),
            ("Leg Press", 3, "10-12", 75, 11),
            ("Walking Lunge", 3, "10-12", 60, 11),
            ("Seated Calf Raise", 3, "12-15", 45, 14),
            ("Plank", 3, "45-60s", 45, None),
        ],
    },
    {
        "focus": "Upper Body — Push & Pull",
        "exercises": [
            ("Incline Dumbbell Press", 4, "8-10", 90, 9),
            ("Seated Cable Row", 3, "8-10", 90, 9),
            ("Dumbbell Shoulder Press", 3, "8-10", 75, 9),
            ("Lat Pulldown", 3, "10-12", 75, 11),
            ("Tricep Pushdown", 3, "10-12", 60, 11),
            ("Face Pull", 3, "12-15", 45, 14),
        ],
    },
    {
        "focus": "Full Body — Posterior Chain",
        "exercises": [
            ("Conventional Deadlift", 4, "5-6", 150, 5),
            ("Dumbbell Bench Press", 3, "8-10", 90, 9),
            ("Bulgarian Split Squat", 3, "8-10 ea", 75, 9),
            ("Dumbbell Row", 3, "10-12", 60, 11),
            ("Barbell Hip Thrust", 3, "10-12", 75, 11),
            ("Push Up", 3, "AMRAP", 60, 14),
        ],
    },
]
DAY_LETTERS = ["A", "B", "C", "D", "E", "F", "G"]

_BASE_KG: dict[Equipment, float | None] = {
    Equipment.BARBELL: 50.0,
    Equipment.DUMBBELL: 16.0,
    Equipment.MACHINE: 40.0,
    Equipment.CABLE: 20.0,
    Equipment.BODYWEIGHT: None,
    Equipment.KETTLEBELL: 16.0,
    Equipment.BAND: None,
    Equipment.OTHER: None,
}
_LEVEL_SCALE = {TrainingLevel.LEVEL_1: 0.8, TrainingLevel.LEVEL_2: 1.0, TrainingLevel.LEVEL_3: 1.25}

# --- Nutrition template ------------------------------------------------------
MEAL_TEMPLATE = [
    ("Breakfast", "🍳", time(7, 30),
     ["3 whole eggs", "1 cup rolled oats", "1 banana"]),
    ("Lunch", "🥗", time(12, 30),
     ["7 oz grilled chicken breast", "1.5 cups rice", "2 cups mixed greens"]),
    ("Afternoon snack", "🥤", time(16, 0),
     ["1 scoop protein powder", "1 handful almonds"]),
    ("Dinner", "🍽️", time(19, 0),
     ["7 oz salmon or lean beef", "1 cup roasted vegetables", "1 medium potato"]),
]
# Roughly how much of the daily target each meal above carries.
_MEAL_SHARE = (0.28, 0.32, 0.10, 0.30)

# --- Pipeline demo data -------------------------------------------------------
LEADS = [
    {
        "full_name": "Alicia Moreno",
        "email": "alicia.moreno@example.com",
        "phone": "+1 512 555 0142",
        "level_interest": TrainingLevel.LEVEL_1,
        "primary_goal": "Lose weight and build a routine",
        "message": "I've never worked with a coach before — a bit nervous but ready to start.",
        "status": LeadStatus.NEW,
    },
    {
        "full_name": "Derek Wu",
        "email": "derek.wu@example.com",
        "phone": "+1 415 555 0198",
        "level_interest": TrainingLevel.LEVEL_2,
        "primary_goal": "Break through a bench press plateau",
        "message": "Training four times a week already — want a real programme instead of winging it.",
        "status": LeadStatus.CONTACTED,
    },
    {
        "full_name": "Fatima Al-Sayed",
        "email": "fatima.alsayed@example.com",
        "phone": None,
        "level_interest": TrainingLevel.LEVEL_1,
        "primary_goal": "General health after a long break",
        "message": "Used to train in college. Haven't touched a weight in three years.",
        "status": LeadStatus.CONVERTED,
    },
    {
        "full_name": "Tom Whitfield",
        "email": "tom.whitfield@example.com",
        "phone": "+1 312 555 0110",
        "level_interest": TrainingLevel.LEVEL_3,
        "primary_goal": "Prep for a powerlifting meet",
        "message": "Meet is in five months. Need a coach who has done this before.",
        "status": LeadStatus.CLOSED,
    },
]

BOOKINGS = [
    {
        "name": "Alicia Moreno",
        "email": "alicia.moreno@example.com",
        "phone": "+1 512 555 0142",
        "days_ahead": 3,
        "topic": "First consultation — goals and schedule",
        "status": BookingStatus.REQUESTED,
        "coach_notes": None,
    },
    {
        "name": "Derek Wu",
        "email": "derek.wu@example.com",
        "phone": "+1 415 555 0198",
        "days_ahead": 1,
        "topic": "Programme walkthrough before starting",
        "status": BookingStatus.CONFIRMED,
        "coach_notes": None,
    },
    {
        "name": "Priya Kapoor",
        "email": "priya.kapoor@example.com",
        "phone": None,
        "days_ahead": -14,
        "topic": "Quarterly check-in call",
        "status": BookingStatus.COMPLETED,
        "coach_notes": (
            "Great progress on posterior chain strength. Talked through adding a peak week "
            "ahead of her next assessment."
        ),
    },
]


async def _exists(db: AsyncSession, model, **filters) -> bool:
    stmt = select(func.count()).select_from(model).filter_by(**filters)
    return bool((await db.execute(stmt)).scalar_one())


async def seed_catalog(db: AsyncSession) -> None:
    """The three tiers, the exercise library and the testimonials.

    Real business content, not test data — always seeded, in every
    environment, including production.
    """
    for index, data in enumerate(PROGRAMS):
        if await _exists(db, Program, slug=data["slug"]):
            continue
        db.add(Program(**data, sort_order=index, is_active=True, is_accepting_clients=True))

    # The exercise library comes from the shipped catalogue in
    # app.data.exercise_library, not from a list in this file.
    #
    # A hand-rolled loop used to live here, inserting Exercise rows with
    # string literals for muscle_group/mechanics/force_type — "General",
    # "Compound", "Push/Pull" — none of which are members of the enums those
    # columns hold. That was fine for a while because the columns used to be
    # plain strings; migration 0005 turned them into native Postgres enums and
    # this loop was never updated to match, so every row it inserted was
    # invalid the moment it was read back, surfacing as a `LookupError` deep
    # in SQLAlchemy's result-row processing the next time anything selected
    # from `exercises` — which is every plan the coach opens.
    #
    # `sync_catalog` is idempotent and additive: it inserts what's missing
    # from `CATALOG` and backfills blank columns on existing rows, but it
    # never overwrites a video link or classification a coach has since edited
    # by hand. Safe to call on every startup, on a fresh database or one
    # that's already partially seeded.
    catalog_report = await sync_catalog(db)
    log.info(
        "seed.catalog_synced",
        created=catalog_report.created,
        backfilled=catalog_report.backfilled,
        unchanged=catalog_report.unchanged,
    )

    for index, (name, level, rating, weeks, quote, metric) in enumerate(TESTIMONIALS):
        if await _exists(db, Testimonial, client_name=name):
            continue
        db.add(
            Testimonial(
                client_name=name,
                level_label=level,
                rating=rating,
                weeks_in=weeks,
                quote=quote,
                result_metric=metric,
                sort_order=index,
            )
        )

    await db.flush()
    log.info("seed.catalog_ready")


def _resolve_coach_password() -> str | None:
    """The password to hash for a *newly created* coach account.

    Never invents a real credential in production. If `COACH_PASSWORD` is not
    set there, this returns `None` and the caller skips creating the account
    entirely.
    """
    if settings.COACH_PASSWORD:
        return settings.COACH_PASSWORD
    if settings.is_production:
        return None
    log.warning("seed.coach_using_dev_fallback_password", email=settings.COACH_EMAIL)
    return DEV_FALLBACK_COACH_PASSWORD


async def seed_coach(db: AsyncSession) -> User | None:
    """The single coach-and-admin account.

    Credentials come from `settings.COACH_EMAIL` / `settings.COACH_PASSWORD`.
    On an existing account, only the role is self-healed here; the password is
    left alone, so a routine reseed can never silently undo a credential the
    coach rotated since. To deliberately set or reset the password, use
    `python -m app.cli create-coach`.
    """
    email = settings.COACH_EMAIL.strip().lower()

    coach = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if coach is not None:
        if coach.role is not UserRole.ADMIN:
            coach.role = UserRole.ADMIN
            log.info("seed.coach_promoted", email=email)
        return coach

    password = _resolve_coach_password()
    if password is None:
        log.error(
            "seed.coach_skipped_no_password",
            email=email,
            hint="Set COACH_PASSWORD, or run: python -m app.cli create-coach",
        )
        return None

    coach = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=COACH_DISPLAY_NAME,
        display_name=COACH_DISPLAY_NAME,
        role=UserRole.ADMIN,
        is_active=True,
        is_verified=True,
    )
    db.add(coach)
    await db.flush()
    log.info("seed.coach_created", email=email)
    return coach


# --- Per-client content -------------------------------------------------------
# Each of these is independently guarded, so they backfill correctly onto a
# client account that already exists.


async def _seed_training(
    db: AsyncSession,
    client: User,
    program: Program,
    exercise_by_name: dict[str, Exercise],
) -> None:
    """A real assigned block: days, movements, sets and rep ranges, plus a few
    weeks of completed sessions so adherence and volume are not all zero."""
    plan = WorkoutPlan(
        client_id=client.id,
        program_id=program.id,
        name=f"{program.name.split(' — ')[0]} Block",
        level=program.level,
        week_number=1,
        total_weeks=12,
        notes="Standard rotation. Swap a movement any week if something is nagging.",
        is_custom=False,
        is_active=True,
    )
    db.add(plan)
    await db.flush()

    scale = _LEVEL_SCALE[program.level]
    days: list[WorkoutDay] = []

    for i in range(program.days_per_week):
        template = WORKOUT_DAY_TEMPLATES[i % len(WORKOUT_DAY_TEMPLATES)]
        day = WorkoutDay(
            plan_id=plan.id,
            label=f"Day {DAY_LETTERS[i]}",
            focus=template["focus"],
            day_of_week=i,
            order_index=i,
            estimated_minutes=program.session_minutes,
        )
        db.add(day)
        await db.flush()

        for order, (name, sets, reps, rest, _nominal) in enumerate(template["exercises"]):
            exercise = exercise_by_name.get(name)
            if exercise is None:
                continue
            base = _BASE_KG.get(exercise.equipment)
            target_weight = round(base * scale, 1) if base is not None else None
            db.add(
                WorkoutDayExercise(
                    day_id=day.id,
                    exercise_id=exercise.id,
                    order_index=order,
                    sets=sets,
                    rep_range=reps,
                    rest_seconds=rest,
                    target_weight_kg=target_weight,
                )
            )
        days.append(day)
    await db.flush()

    # Up to three weeks of completed sessions, three days a week, so the
    # dashboard's "sessions this week" and volume numbers have real data.
    today = date.today()
    for week_offset in range(3, 0, -1):
        for day in days[: min(len(days), 3)]:
            session_date = today - timedelta(weeks=week_offset, days=6 - day.order_index)
            session = WorkoutSession(
                client_id=client.id,
                day_id=day.id,
                session_date=session_date,
                status=SessionStatus.COMPLETED,
                duration_minutes=day.estimated_minutes,
                completed_at=datetime.combine(session_date, time(18, 0), tzinfo=UTC),
            )
            db.add(session)
            await db.flush()

            day_exercises = (
                await db.execute(
                    select(WorkoutDayExercise).where(WorkoutDayExercise.day_id == day.id)
                )
            ).scalars().all()
            template = WORKOUT_DAY_TEMPLATES[day.order_index % len(WORKOUT_DAY_TEMPLATES)]
            nominal_by_order = {i: ex[4] for i, ex in enumerate(template["exercises"])}

            for de in day_exercises:
                reps = nominal_by_order.get(de.order_index)
                for set_number in range(1, de.sets + 1):
                    db.add(
                        SetLog(
                            session_id=session.id,
                            day_exercise_id=de.id,
                            set_number=set_number,
                            weight_kg=de.target_weight_kg,
                            reps=reps,
                            is_completed=True,
                        )
                    )
    await db.flush()


async def _seed_meal_plan(db: AsyncSession, client: User, spec: dict) -> None:
    """A seven-day meal plan built from the client's own macro targets."""
    calories = spec["calorie_target"] or 2000
    protein = spec["protein_target_g"] or 140
    carbs = spec["carb_target_g"] or 200
    fat = spec["fat_target_g"] or 60

    plan = MealPlan(
        client_id=client.id,
        name="Weekly Meal Plan",
        phase=spec["goal"],
        calorie_target=calories,
        protein_target_g=protein,
        carb_target_g=carbs,
        fat_target_g=fat,
        notes="Hit protein first, then fill the rest of the day with whatever fits.",
        is_active=True,
    )
    db.add(plan)
    await db.flush()

    for day_of_week in range(7):
        for order, (name, icon, serve_time, items) in enumerate(MEAL_TEMPLATE):
            share = _MEAL_SHARE[order]
            meal = Meal(
                plan_id=plan.id,
                day_of_week=day_of_week,
                order_index=order,
                name=name,
                serve_time=serve_time,
                icon=icon,
                calories=round(calories * share),
                protein_g=round(protein * share),
                carbs_g=round(carbs * share),
                fat_g=round(fat * share),
            )
            db.add(meal)
            await db.flush()
            for item_order, label in enumerate(items):
                db.add(MealItem(meal_id=meal.id, label=label, order_index=item_order))
    await db.flush()


async def _seed_wellness(db: AsyncSession, client: User) -> None:
    """Three weeks of sleep and a rotating handful of cardio sessions."""
    today = date.today()

    for offset in range(21):
        log_date = today - timedelta(days=offset)
        hours = round(6.5 + ((offset * 37) % 5) / 2, 1)  # 6.5–9.0, varied but deterministic
        quality = 3 + ((offset * 13) % 3)
        db.add(
            SleepLog(
                client_id=client.id,
                log_date=log_date,
                bedtime=time(22, 30),
                wake_time=time(6, 30),
                hours_slept=hours,
                quality=quality,
            )
        )

    activities = [CardioType.WALKING, CardioType.RUNNING, CardioType.CYCLING]
    for i, offset in enumerate(range(1, 21, 3)):
        log_date = today - timedelta(days=offset)
        activity = activities[i % len(activities)]
        duration = 25 + (i % 3) * 10
        distance = round(duration / 4, 1) if activity is CardioType.CYCLING else round(duration / 12, 1)
        db.add(
            CardioLog(
                client_id=client.id,
                log_date=log_date,
                activity_type=activity,
                duration_minutes=duration,
                distance_km=distance,
                avg_heart_rate=125 + (i % 4) * 5,
                calories_burned=duration * 7,
                intensity=Intensity.MODERATE,
                source=DataSource.MANUAL,
            )
        )
    await db.flush()


async def _seed_measurements(db: AsyncSession, client: User, spec: dict) -> None:
    """Two tape-measurement check-ins, trending in the direction of the goal."""
    today = date.today()
    direction = -1 if spec["goal"] is Goal.CUT else (1 if spec["goal"] is Goal.BUILD else 0)

    for weeks_ago, progressed in ((4, False), (1, True)):
        drift = direction * 1.5 if progressed else 0
        db.add(
            BodyMeasurement(
                client_id=client.id,
                log_date=today - timedelta(weeks=weeks_ago),
                chest_cm=round(98 + drift, 1),
                waist_cm=round(84 + drift, 1),
                hips_cm=round(100 + drift, 1),
                left_arm_cm=round(32 + drift * 0.3, 1),
                right_arm_cm=round(32.2 + drift * 0.3, 1),
                left_thigh_cm=round(56 + drift * 0.5, 1),
                right_thigh_cm=round(56.2 + drift * 0.5, 1),
                neck_cm=round(37 + drift * 0.2, 1),
            )
        )
    await db.flush()


async def _seed_checkin_photo(db: AsyncSession, client: User) -> None:
    """One placeholder check-in photo, written through the real storage path
    so the existing authenticated photo route serves it correctly.

    This is a plain labelled placeholder card, not a synthesised body photo —
    it exists to prove the gallery, upload path and signed-URL serving all
    work, not to stand in for a real client image.
    """
    from PIL import Image, ImageDraw

    log_date = date.today() - timedelta(days=3)
    directory = Path(settings.UPLOAD_DIR) / str(client.id) / log_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:16]}.jpg"
    destination = directory / filename

    image = Image.new("RGB", (800, 1000), color=(20, 20, 23))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 40, 760, 960], outline=(229, 32, 44), width=6)
    draw.text((80, 450), "Check-in photo", fill=(255, 255, 255))
    draw.text((80, 480), "(seed placeholder)", fill=(160, 160, 170))
    draw.text((80, 510), log_date.isoformat(), fill=(160, 160, 170))
    image.save(destination, "JPEG", quality=85)

    key = f"{client.id}/{log_date.isoformat()}/{filename}"
    db.add(
        ProgressPhoto(
            client_id=client.id,
            log_date=log_date,
            pose=PhotoPose.FRONT,
            file_key=key,
            content_type="image/jpeg",
            size_bytes=destination.stat().st_size,
            shared_with_coach=True,
        )
    )
    await db.flush()


async def _seed_conversation(db: AsyncSession, client: User, coach: User, spec: dict) -> None:
    """A short, real-looking exchange rather than a single welcome line.

    Each exchange deliberately ends with a client message left unread — the
    same condition the coach dashboard's unread badge and the client
    portal's own notification counter both key off, so the seeded data
    proves that path end to end rather than just existing.
    """
    thread = MessageThread(client_id=client.id, coach_id=coach.id)
    db.add(thread)
    await db.flush()

    first_name = spec["full_name"].split()[0]
    now = datetime.now(UTC)

    if spec["tier"]:
        exchange = [
            (coach.id, f"Welcome, {first_name}! Your first block is loaded — check the "
                       "Workout tab and let me know if anything doesn't make sense.",
             timedelta(days=10)),
            (client.id, "Just finished day one. That cue about controlling the descent made "
                        "a real difference.", timedelta(days=9, hours=20)),
            (coach.id, "That's exactly it. Keep that tempo through the block and we'll add "
                       "weight once it feels automatic.", timedelta(days=9, hours=18)),
            (client.id, "Will do. Also — is it normal for my legs to still be sore two days "
                        "later?", timedelta(hours=5)),
        ]
    else:
        exchange = [
            (coach.id, f"Hi {first_name} — welcome to Coach Auto. Whenever you're ready, "
                       "have a look at the plans and pick whichever fits your week. Happy to "
                       "talk it through first if that's easier.", timedelta(days=2)),
        ]

    for sender_id, body, age in exchange:
        created = now - age
        message = Message(thread_id=thread.id, sender_id=sender_id, body=body, created_at=created)
        if sender_id == coach.id:
            message.read_at = created  # the coach's own messages carry no unread state
        db.add(message)

    thread.last_message_at = now - exchange[-1][2]
    await db.flush()


async def seed_clients(db: AsyncSession, coach: User) -> int:
    """Demo client accounts, and everything attached to them.

    Returns how many *new accounts* were created, for the summary line. Every
    piece attached to a client is checked independently, so this backfills
    correctly onto accounts that already exist.
    """
    exercise_by_name = {
        ex.name: ex for ex in (await db.execute(select(Exercise))).scalars().all()
    }
    created = 0

    for spec in CLIENTS:
        client = (
            await db.execute(select(User).where(User.email == spec["email"]))
        ).scalar_one_or_none()

        is_new = client is None
        if is_new:
            client = User(
                email=spec["email"],
                hashed_password=hash_password(spec["password"]),
                full_name=spec["full_name"],
                display_name=spec["display_name"],
                role=UserRole.CLIENT,
                is_active=True,
                is_verified=True,
            )
            db.add(client)
            await db.flush()
            created += 1

        profile = (
            await db.execute(select(ClientProfile).where(ClientProfile.user_id == client.id))
        ).scalar_one_or_none()
        if profile is None:
            profile = ClientProfile(
                user_id=client.id,
                sex=spec["sex"],
                height_cm=spec["height_cm"],
                starting_weight_kg=spec["start_kg"],
                current_weight_kg=spec["current_kg"],
                goal_weight_kg=spec["goal_kg"],
                goal=spec["goal"],
                activity_level=ActivityLevel.LIGHT,
                unit_system=UnitSystem.IMPERIAL,
                level=None,  # set below, and only via a subscription
                onboarding_completed=spec["tier"] is not None,
            )
            if spec["weeks_in"]:
                profile.program_start_date = date.today() - timedelta(weeks=spec["weeks_in"])
                profile.program_week = min(spec["weeks_in"], 12)
                profile.program_total_weeks = 12
            db.add(profile)
            await db.flush()

        # Backfill macro targets onto an existing profile only if nobody has
        # set them since — never overwrite a value the coach has since edited.
        if spec["tier"] and profile.calorie_target is None and spec["calorie_target"]:
            profile.calorie_target = spec["calorie_target"]
            profile.protein_target_g = spec["protein_target_g"]
            profile.carb_target_g = spec["carb_target_g"]
            profile.fat_target_g = spec["fat_target_g"]

        program = None
        if spec["tier"]:
            program = (
                await db.execute(select(Program).where(Program.slug == spec["tier"]))
            ).scalar_one_or_none()

        if program is not None:
            profile.weekly_workout_target = program.days_per_week

            if not await _exists(
                db, Subscription, client_id=client.id, program_id=program.id,
                status=SubscriptionStatus.ACTIVE,
            ):
                started = datetime.now(UTC) - timedelta(weeks=spec["weeks_in"] or 1)
                db.add(
                    Subscription(
                        client_id=client.id,
                        program_id=program.id,
                        status=SubscriptionStatus.ACTIVE,
                        price_cents=program.price_cents,
                        currency="usd",
                        started_at=started,
                        current_period_start=datetime.now(UTC) - timedelta(days=7),
                        current_period_end=datetime.now(UTC) + timedelta(days=23),
                        # No Stripe ids: this is seeded, not bought. The
                        # billing portal correctly refuses to open for it.
                    )
                )
                await db.flush()
                # Derive the level the same way the webhook does, rather than
                # setting it by hand — one code path, one set of bugs.
                await entitlements.sync_profile_level(db, client.id)

            if not await _exists(db, WorkoutPlan, client_id=client.id):
                await _seed_training(db, client, program, exercise_by_name)

            if not await _exists(db, MealPlan, client_id=client.id):
                await _seed_meal_plan(db, client, spec)

            if not await _exists(db, SleepLog, client_id=client.id):
                await _seed_wellness(db, client)

            if not await _exists(db, BodyMeasurement, client_id=client.id):
                await _seed_measurements(db, client, spec)

            if not await _exists(db, ProgressPhoto, client_id=client.id):
                await _seed_checkin_photo(db, client)

            if not await _exists(db, WeightLog, client_id=client.id):
                if spec["start_kg"] and spec["current_kg"] and spec["weeks_in"]:
                    weeks = spec["weeks_in"]
                    step = (spec["current_kg"] - spec["start_kg"]) / weeks
                    today = date.today()
                    for week in range(weeks + 1):
                        db.add(
                            WeightLog(
                                client_id=client.id,
                                log_date=today - timedelta(weeks=weeks - week),
                                weight_kg=round(spec["start_kg"] + step * week, 1),
                            )
                        )
                    await db.flush()

        if not await _exists(db, MessageThread, client_id=client.id):
            await _seed_conversation(db, client, coach, spec)

        log.info("seed.client_ready", email=spec["email"], created=is_new, tier=spec["tier"])

    return created


async def seed_pipeline(db: AsyncSession) -> None:
    """Website enquiries and consultation bookings.

    Kept as clearly different content on purpose: a lead is someone who asked
    a question and has not necessarily talked to anyone yet; a booking is a
    call actually on the calendar, in the past, present or future. Real,
    distinct sample rows in each make that difference visible on the two
    dashboard screens rather than something you have to take on faith.
    """
    for spec in LEADS:
        if await _exists(db, Lead, email=spec["email"]):
            continue
        db.add(
            Lead(
                full_name=spec["full_name"],
                email=spec["email"],
                phone=spec["phone"],
                level_interest=spec["level_interest"],
                primary_goal=spec["primary_goal"],
                message=spec["message"],
                source="website",
                status=spec["status"],
                consent_marketing=True,
            )
        )

    now = datetime.now(UTC)
    for spec in BOOKINGS:
        if await _exists(db, ConsultationBooking, email=spec["email"], topic=spec["topic"]):
            continue
        db.add(
            ConsultationBooking(
                name=spec["name"],
                email=spec["email"],
                phone=spec["phone"],
                preferred_at=now + timedelta(days=spec["days_ahead"]),
                timezone="America/Chicago",
                topic=spec["topic"],
                status=spec["status"],
                coach_notes=spec["coach_notes"],
            )
        )

    await db.flush()
    log.info("seed.pipeline_ready")


def _should_seed_demo_clients() -> bool:
    """Seeded everywhere by default, including production.

    `SEED_DEMO_CLIENTS=false` is the only way to opt out, in any environment.
    There used to be an environment-based default here — demo clients on
    automatically outside production, off automatically inside it — which
    meant getting real demo data in front of an actual client required
    remembering a one-off override every single time. A handful of clearly
    fake `@example.com` accounts alongside real customers has never been the
    problem; being unable to get them without a flag was.
    """
    return settings.SEED_DEMO_CLIENTS


async def run_seed(db: AsyncSession) -> None:
    """Seed everything, and commit it."""
    await seed_catalog(db)
    coach = await seed_coach(db)

    demo_clients_created = 0
    seed_demo = _should_seed_demo_clients()
    if coach is not None and seed_demo:
        demo_clients_created = await seed_clients(db, coach)
        await seed_pipeline(db)
    elif not seed_demo:
        log.info("seed.demo_clients_skipped", reason="SEED_DEMO_CLIENTS=false")

    await db.commit()

    log.info(
        "seed.complete",
        environment=settings.ENVIRONMENT,
        coach=settings.COACH_EMAIL if coach else "SKIPPED — see seed.coach_skipped_no_password",
        demo_clients_created=demo_clients_created,
    )