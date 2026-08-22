"""The one and only seed.

Run with `python -m app.cli seed`. Every insert is guarded by an existence
check, so running it twice changes nothing.

What it creates:

  * the three coaching tiers (Level 1, 2 and 3) with their public copy
  * the exercise library and the published testimonials
  * ONE staff account — the coach, who is also the super admin
  * outside production only: a handful of demo client accounts covering the
    states worth testing — a brand-new sign-up with no plan, and subscribed
    clients on each tier

Two things this file got wrong before, both now fixed:

1. It never committed. Every step called `db.flush()`, which pushes SQL to the
   database and makes it visible *within the current transaction* — enough to
   make later existence checks and log lines behave correctly, and enough to
   look completely successful. But `flush` is not `commit`, and the session
   this runs under is a raw `SessionLocal()`, not the `get_db()` FastAPI
   dependency that commits automatically on a clean request. Without an
   explicit commit, closing the session at the end of the script rolled the
   whole transaction back — the coach, the demo clients, all of it. The
   terminal said "seed.coach_created"; the database never actually got a row.

2. The coach's credentials were hardcoded here instead of read from
   `settings.COACH_EMAIL` / `settings.COACH_PASSWORD` — i.e. from the
   environment, which is where a real production credential belongs, not in a
   file that ends up in source control. Whatever was typed at the CLI's old
   "Coach password" prompt was silently discarded either way; that prompt is
   gone now, because it was ignored.

A note on levels. A client's level comes from a paid subscription, not from a
column somebody set by hand. The seeded clients therefore get a real
`Subscription` row apiece; the profile level is derived from it through the
same `entitlements` code the webhook uses, so seeded data and production data
travel identical paths. The brand-new sign-up has no subscription and no
level, which is exactly what the portal should treat as "choose a plan".
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.billing import Subscription
from app.models.catalog import Exercise, Program, Testimonial
from app.models.engagement import Message, MessageThread
from app.models.enums import (
    ActivityLevel,
    Equipment,
    Goal,
    Sex,
    SubscriptionStatus,
    TrainingLevel,
    UnitSystem,
    UserRole,
)
from app.models.tracking import WeightLog
from app.models.user import ClientProfile, User
from app.services import entitlements

log = get_logger("seed")

# The legal business name never appears in the product. Everything public
# says Coach Auto. The account's own login email comes from settings.
COACH_DISPLAY_NAME = "Coach Auto"

# Used only outside production, and only when `settings.COACH_PASSWORD` is
# unset there too — so a laptop clone of this repo has a working coach login
# on the first `seed` run without anyone needing to write a .env first. This
# value is never used in production; see `_resolve_coach_password` below.
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

# --- Exercise library ----------------------------------------------------------
# name, target, secondary, equipment, cue
EXERCISES = [
    ("Barbell Back Squat", "Quads", ["Glutes", "Core"], Equipment.BARBELL,
     "Control the descent — three seconds down."),
    ("Romanian Deadlift", "Hamstrings", ["Glutes", "Lower back"], Equipment.BARBELL,
     "Push the hips back. The bar stays against your legs."),
    ("Leg Press", "Quads", ["Glutes"], Equipment.MACHINE,
     "Feet shoulder-width. Do not let your lower back round off the pad."),
    ("Walking Lunge", "Quads", ["Glutes", "Hamstrings"], Equipment.DUMBBELL,
     "Long stride. Back knee tracks down, not forward."),
    ("Seated Calf Raise", "Calves", ["Soleus"], Equipment.MACHINE,
     "Full stretch at the bottom, one-second squeeze at the top."),
    ("Incline Dumbbell Press", "Upper chest", ["Shoulders", "Triceps"], Equipment.DUMBBELL,
     "30° incline, controlled lowering."),
    ("Seated Cable Row", "Lats", ["Rhomboids", "Biceps"], Equipment.CABLE,
     "Chest tall. Drive the elbows past your ribs."),
    ("Dumbbell Shoulder Press", "Shoulders", ["Triceps"], Equipment.DUMBBELL,
     "Ribs down. Do not arch the lower back to finish the rep."),
    ("Lat Pulldown", "Lats", ["Biceps"], Equipment.CABLE,
     "Pull to the collarbone, not behind the neck."),
    ("Tricep Pushdown", "Triceps", [], Equipment.CABLE,
     "Elbows pinned to your sides throughout."),
    ("Conventional Deadlift", "Posterior chain", ["Glutes", "Lats"], Equipment.BARBELL,
     "Brace the core, flat back throughout."),
    ("Dumbbell Bench Press", "Chest", ["Triceps", "Shoulders"], Equipment.DUMBBELL,
     "Wrists stacked over elbows at the bottom."),
    ("Bulgarian Split Squat", "Quads", ["Glutes"], Equipment.DUMBBELL,
     "Front shin near vertical. Weight through the mid-foot."),
    ("Dumbbell Row", "Lats", ["Rhomboids", "Biceps"], Equipment.DUMBBELL,
     "Row to the hip, not the armpit."),
    ("Plank", "Core", ["Shoulders"], Equipment.BODYWEIGHT,
     "Squeeze glutes. A straight line from ear to ankle."),
    ("Hip Thrust", "Glutes", ["Hamstrings"], Equipment.BARBELL,
     "Chin tucked. Finish with the hips level, not hyperextended."),
    ("Dumbbell Lateral Raise", "Shoulders", [], Equipment.DUMBBELL,
     "Lead with the elbow. Stop at shoulder height."),
    ("Push-Up", "Chest", ["Triceps", "Core"], Equipment.BODYWEIGHT,
     "Body moves as one piece. Hips do not sag."),
    ("Face Pull", "Rear delts", ["Rotator cuff"], Equipment.CABLE,
     "Pull to the forehead, thumbs back at the finish."),
    ("Leg Curl", "Hamstrings", [], Equipment.MACHINE,
     "Hips stay down on the pad the whole set."),
]

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
# Deliberately varied, so every state the portal has to render exists in a
# fresh database: no plan at all, and one client on each tier. Seeded outside
# production by default — see `_should_seed_demo_clients` for the override.
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
    },
    {
        # The important one. Signed up, never paid: no subscription, no level,
        # and the portal should be showing them the plans rather than a
        # programme.
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

    for name, target, secondary, equipment, cue in EXERCISES:
        slug = slugify(name)
        if await _exists(db, Exercise, slug=slug):
            continue
        db.add(
            Exercise(
                slug=slug,
                name=name,
                target_muscle=target,
                secondary_muscles=secondary,
                equipment=equipment,
                coaching_cue=cue,
                video_url=None,  # the coach adds links from the dashboard
                min_level=TrainingLevel.LEVEL_1,
            )
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
    entirely — a missing coach is a loud, obvious problem to fix by setting
    the environment variable; a coach account quietly created with a made-up
    password is a security incident waiting to be discovered.
    """
    if settings.COACH_PASSWORD:
        return settings.COACH_PASSWORD
    if settings.is_production:
        return None
    log.warning("seed.coach_using_dev_fallback_password", email=settings.COACH_EMAIL)
    return DEV_FALLBACK_COACH_PASSWORD


async def seed_coach(db: AsyncSession) -> User | None:
    """The single coach-and-admin account.

    Credentials come from `settings.COACH_EMAIL` / `settings.COACH_PASSWORD` —
    the environment — never from a constant in this file. On an existing
    account, only the role is self-healed here; the password is left alone on
    purpose, so a routine reseed can never silently undo a credential the
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


async def seed_clients(db: AsyncSession, coach: User) -> int:
    """Demo client accounts, their subscriptions, and a starting conversation.

    Returns how many were newly created, for the summary line in `run_seed`.
    """
    today = date.today()
    created = 0

    for spec in CLIENTS:
        if await _exists(db, User, email=spec["email"]):
            continue

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
            # Left NULL on purpose — set below, and only via a subscription.
            level=None,
            onboarding_completed=spec["tier"] is not None,
        )
        if spec["weeks_in"]:
            profile.program_start_date = today - timedelta(weeks=spec["weeks_in"])
            profile.program_week = spec["weeks_in"]
            profile.program_total_weeks = 12
        db.add(profile)
        await db.flush()

        # A paid tier means a subscription row, exactly as Stripe would create.
        if spec["tier"]:
            program = (
                await db.execute(select(Program).where(Program.slug == spec["tier"]))
            ).scalar_one_or_none()

            if program is not None:
                started = datetime.now(UTC) - timedelta(weeks=spec["weeks_in"] or 1)
                db.add(
                    Subscription(
                        client_id=client.id,
                        program_id=program.id,
                        status=SubscriptionStatus.ACTIVE,
                        price_cents=program.price_cents,
                        currency="usd",
                        billing_period=program.billing_period,
                        started_at=started,
                        current_period_start=datetime.now(UTC) - timedelta(days=7),
                        current_period_end=datetime.now(UTC) + timedelta(days=23),
                        # No Stripe ids: these are seeded, not bought. The
                        # billing portal correctly refuses to open for them.
                    )
                )
                await db.flush()
                # Derive the level the same way the webhook does, rather than
                # setting it by hand — one code path, one set of bugs.
                await entitlements.sync_profile_level(db, client.id)

        # Some weight history, so the progress chart is not an empty box.
        if spec["start_kg"] and spec["current_kg"] and spec["weeks_in"]:
            weeks = spec["weeks_in"]
            step = (spec["current_kg"] - spec["start_kg"]) / weeks
            for week in range(weeks + 1):
                db.add(
                    WeightLog(
                        client_id=client.id,
                        log_date=today - timedelta(weeks=weeks - week),
                        weight_kg=round(spec["start_kg"] + step * week, 1),
                    )
                )

        # Open the thread with a welcome, so messaging is never a blank screen.
        thread = MessageThread(client_id=client.id, coach_id=coach.id)
        db.add(thread)
        await db.flush()
        db.add(
            Message(
                thread_id=thread.id,
                sender_id=coach.id,
                body=(
                    f"Welcome, {spec['full_name'].split()[0]}. Fill in your intake when you get "
                    "a minute and I will have your first block ready. Message me here with "
                    "anything at all."
                ),
            )
        )
        thread.last_message_at = datetime.now(UTC)
        await db.flush()

        log.info("seed.client_created", email=spec["email"], tier=spec["tier"])
        created += 1

    return created


def _should_seed_demo_clients() -> bool:
    """Whether to create the four demo/test client accounts.

    Defaults to "yes everywhere except production" — a live customer database
    is not the place for `sandra.thompson@example.com` to show up uninvited.
    `SEED_DEMO_CLIENTS` overrides the default explicitly in either direction,
    for a staging environment that is technically `ENVIRONMENT=production`
    but should still get sample data, or the reverse.
    """
    if settings.SEED_DEMO_CLIENTS is not None:
        return settings.SEED_DEMO_CLIENTS
    return not settings.is_production


async def run_seed(db: AsyncSession) -> None:
    """Seed everything, and commit it.

    That commit is the fix: every step before this used to only `flush()`,
    which is visible within the transaction but is not durable. Closing the
    session without an explicit commit rolled the entire seed back — silently,
    since flush makes it look like it worked right up until the process exits.
    """
    await seed_catalog(db)
    coach = await seed_coach(db)

    demo_clients_created = 0
    seed_demo = _should_seed_demo_clients()
    if coach is not None and seed_demo:
        demo_clients_created = await seed_clients(db, coach)
    elif not seed_demo:
        log.info("seed.demo_clients_skipped", reason="not enabled for this environment")

    await db.commit()

    log.info(
        "seed.complete",
        environment=settings.ENVIRONMENT,
        coach=settings.COACH_EMAIL if coach else "SKIPPED — see seed.coach_skipped_no_password",
        demo_clients_created=demo_clients_created,
    )