"""Idempotent seed data.

Run with `python -m app.cli seed`. Safe to run repeatedly — every insert is
guarded by an existence check, so it will not duplicate rows.
"""

from datetime import date, time, timedelta

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.catalog import Exercise, Program, Testimonial
from app.models.enums import (
    ActivityLevel,
    CardioType,
    Equipment,
    Goal,
    Intensity,
    Sex,
    TrainingLevel,
    UnitSystem,
    UserRole,
)
from app.models.nutrition import Meal, MealItem, MealPlan
from app.models.tracking import BodyMeasurement, CardioLog, SleepLog, WeightLog
from app.models.training import WorkoutDay, WorkoutDayExercise, WorkoutPlan
from app.models.user import ClientProfile, User

log = get_logger("seed")

# --- Programs ------------------------------------------------------------------
# Beginner 3 days, Intermediate 4 days, Advanced 5–6 days. All tiers are priced
# the same at launch; existing clients keep their rate when prices rise.
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


async def _exists(db: AsyncSession, model, **filters) -> bool:
    stmt = select(func.count()).select_from(model).filter_by(**filters)
    return bool((await db.execute(stmt)).scalar_one())


async def seed_catalog(db: AsyncSession) -> None:
    """Programs, exercises and published testimonials."""
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
                video_url=None,  # Coach adds the YouTube/Vimeo link from the dashboard
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


async def seed_coach(db: AsyncSession, password: str) -> User:
    existing = (
        await db.execute(select(User).where(User.email == settings.COACH_EMAIL))
    ).scalar_one_or_none()
    if existing:
        return existing

    coach = User(
        email=settings.COACH_EMAIL,
        hashed_password=hash_password(password),
        # The legal name stays out of the product. Everything public says "Coach Auto".
        full_name="Coach Auto",
        display_name="Coach Auto",
        role=UserRole.COACH,
        is_active=True,
        is_verified=True,
    )
    db.add(coach)
    await db.flush()
    log.info("seed.coach_created", email=settings.COACH_EMAIL)
    return coach


async def seed_demo_client(db: AsyncSession, coach: User, password: str) -> None:
    """A worked example so the portal is never an empty shell on first run."""
    email = "demo.client@autonomyfitness.press"
    if await _exists(db, User, email=email):
        return

    today = date.today()
    client = User(
        email=email,
        hashed_password=hash_password(password),
        full_name="Sandra Thompson",
        display_name="Sandra T.",
        role=UserRole.CLIENT,
        is_active=True,
        is_verified=True,
    )
    db.add(client)
    await db.flush()

    db.add(
        ClientProfile(
            user_id=client.id,
            sex=Sex.FEMALE,
            date_of_birth=date(today.year - 38, 4, 12),
            height_cm=165,
            starting_weight_kg=72.0,
            current_weight_kg=69.85,
            goal_weight_kg=67.1,
            goal=Goal.CUT,
            activity_level=ActivityLevel.LIGHT,
            unit_system=UnitSystem.IMPERIAL,
            level=TrainingLevel.LEVEL_1,
            phase="Cut Phase",
            program_start_date=today - timedelta(weeks=7),
            program_week=8,
            program_total_weeks=12,
            calorie_target=2140,
            protein_target_g=160,
            carb_target_g=220,
            fat_target_g=55,
            weekly_workout_target=3,
            onboarding_completed=True,
        )
    )

    # --- Training block --------------------------------------------------------
    exercises = {
        e.name: e for e in (await db.execute(select(Exercise))).scalars().all()
    }
    plan = WorkoutPlan(
        client_id=client.id,
        assigned_by_id=coach.id,
        name="Level 1 — Strength Foundation",
        level=TrainingLevel.LEVEL_1,
        week_number=8,
        total_weeks=12,
        is_active=True,
    )
    db.add(plan)
    await db.flush()

    blueprint = [
        ("Day A", "Lower Body", 0, [
            ("Barbell Back Squat", 4, "8-10", 90),
            ("Romanian Deadlift", 3, "10-12", 90),
            ("Leg Press", 3, "12-15", 60),
            ("Walking Lunge", 3, "12 / leg", 60),
            ("Seated Calf Raise", 4, "15-20", 45),
        ]),
        ("Day B", "Upper Body", 2, [
            ("Incline Dumbbell Press", 4, "8-10", 90),
            ("Seated Cable Row", 3, "10-12", 90),
            ("Dumbbell Shoulder Press", 3, "10-12", 90),
            ("Lat Pulldown", 3, "12-15", 60),
            ("Tricep Pushdown", 3, "12-15", 45),
        ]),
        ("Day C", "Full Body", 4, [
            ("Conventional Deadlift", 4, "6-8", 120),
            ("Dumbbell Bench Press", 3, "10-12", 90),
            ("Bulgarian Split Squat", 3, "10 / leg", 90),
            ("Dumbbell Row", 3, "12 / arm", 60),
            ("Plank", 3, "45-60 sec", 30),
        ]),
    ]

    for day_index, (label, focus, dow, movements) in enumerate(blueprint):
        day = WorkoutDay(
            plan_id=plan.id, label=label, focus=focus, day_of_week=dow, order_index=day_index
        )
        db.add(day)
        await db.flush()
        for order, (name, sets, reps, rest) in enumerate(movements):
            exercise = exercises.get(name)
            if exercise is None:
                continue
            db.add(
                WorkoutDayExercise(
                    day_id=day.id,
                    exercise_id=exercise.id,
                    order_index=order,
                    sets=sets,
                    rep_range=reps,
                    rest_seconds=rest,
                    coach_note=exercise.coaching_cue,
                )
            )

    # --- Meal plan -------------------------------------------------------------
    meal_plan = MealPlan(
        client_id=client.id,
        assigned_by_id=coach.id,
        name="Level 1 — Cut Phase",
        phase=Goal.CUT,
        calorie_target=2140,
        protein_target_g=160,
        carb_target_g=220,
        fat_target_g=55,
    )
    db.add(meal_plan)
    await db.flush()

    day_meals = [
        ("Morning Power Bowl", time(7, 0), "🥣", 494, 42, 55, 12,
         ["1 cup oats", "1 scoop whey protein", "1 banana", "1 tbsp almond butter",
          "½ cup blueberries"]),
        ("Pre-Workout Meal", time(12, 0), "🍗", 418, 38, 48, 8,
         ["5oz grilled chicken", "¾ cup white rice", "1 cup green beans", "1 tbsp olive oil"]),
        ("Post-Workout Shake", time(15, 30), "🥤", 356, 50, 30, 4,
         ["2 scoops whey protein", "1 cup almond milk", "1 banana", "5g creatine"]),
        ("Dinner", time(19, 0), "🍽️", 475, 45, 40, 15,
         ["6oz lean beef or salmon", "1 cup roasted sweet potato", "2 cups salad",
          "1 tbsp dressing"]),
    ]
    for dow in range(7):
        for order, (name, at, icon, kcal, p, c, f, items) in enumerate(day_meals):
            meal = Meal(
                plan_id=meal_plan.id,
                day_of_week=dow,
                order_index=order,
                name=name,
                serve_time=at,
                icon=icon,
                calories=kcal,
                protein_g=p,
                carbs_g=c,
                fat_g=f,
            )
            db.add(meal)
            await db.flush()
            for item_order, label in enumerate(items):
                db.add(MealItem(meal_id=meal.id, label=label, order_index=item_order))

    # --- Seven weeks of history -----------------------------------------------
    weights = [72.0, 71.7, 71.4, 71.1, 70.8, 70.4, 70.2, 69.85]
    for weeks_ago, kg in enumerate(reversed(weights)):
        db.add(
            WeightLog(
                client_id=client.id,
                log_date=today - timedelta(weeks=weeks_ago),
                weight_kg=kg,
            )
        )

    db.add(
        BodyMeasurement(
            client_id=client.id,
            log_date=today - timedelta(weeks=7),
            chest_cm=95.3, waist_cm=77.5, hips_cm=101.6,
            left_arm_cm=31.8, right_arm_cm=31.8, left_thigh_cm=59.7,
        )
    )
    db.add(
        BodyMeasurement(
            client_id=client.id,
            log_date=today,
            chest_cm=91.4, waist_cm=71.1, hips_cm=96.5,
            left_arm_cm=33.0, right_arm_cm=33.0, left_thigh_cm=55.9,
        )
    )

    sleep_hours = [7.5, 6.8, 8.1, 7.2, 7.9, 6.5, 8.4, 7.7, 7.1, 8.0, 6.9, 7.6, 8.2, 7.4]
    for days_ago, hours in enumerate(sleep_hours):
        db.add(
            SleepLog(
                client_id=client.id,
                log_date=today - timedelta(days=days_ago),
                bedtime=time(22, 45),
                wake_time=time(6, 30),
                hours_slept=hours,
                quality=4 if hours >= 7.5 else 3,
            )
        )

    cardio = [
        (1, CardioType.WALKING, 45, Intensity.LOW, 180),
        (3, CardioType.ELLIPTICAL, 30, Intensity.MODERATE, 240),
        (5, CardioType.WALKING, 40, Intensity.LOW, 160),
        (8, CardioType.CYCLING, 35, Intensity.MODERATE, 290),
        (10, CardioType.HIIT, 20, Intensity.HIGH, 260),
    ]
    for days_ago, activity, minutes, intensity, kcal in cardio:
        db.add(
            CardioLog(
                client_id=client.id,
                log_date=today - timedelta(days=days_ago),
                activity_type=activity,
                duration_minutes=minutes,
                intensity=intensity,
                calories_burned=kcal,
            )
        )

    await db.flush()
    log.info("seed.demo_client_created", email=email)


async def run_seed(db: AsyncSession, coach_password: str, demo_password: str) -> None:
    await seed_catalog(db)
    coach = await seed_coach(db, coach_password)
    await seed_demo_client(db, coach, demo_password)
    await db.commit()
