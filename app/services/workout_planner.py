"""
Automatic training blocks, built from what the client told us about themselves.

The coach cannot write a bespoke block for every new client the minute they
pay, and a client who opens an empty Workout tab on day one usually does not
come back. So the moment their intake is in, this builds a real, trainable
block for them — and the coach replaces or edits it whenever they want. Manual
and automatic are not rival systems here: an automatic plan is a starting
point that the coach overwrites at the first check-in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.models.catalog import Exercise
from app.models.enums import (
    Equipment,
    Goal,
    Mechanics,
    MuscleGroup,
    TrainingExperience,
    TrainingLevel,
    TrainingLocation,
)
from app.models.training import WorkoutDay, WorkoutDayExercise, WorkoutPlan
from app.models.user import ClientProfile
from app.services.programming import build_video_index

log = get_logger("training.planner")

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"


class WorkoutPlanInputError(ValueError):
    """The intake is missing something the plan cannot be built without."""


# =============================================================================
# Prescription: how hard, how many, how long between sets
# =============================================================================


@dataclass(frozen=True, slots=True)
class Prescription:
    sets: int
    rep_range: str
    rest_seconds: int


@dataclass(frozen=True, slots=True)
class GoalStyle:
    compound: Prescription
    isolation: Prescription
    conditioning_minutes: int
    summary: str


GOAL_STYLES: dict[Goal, GoalStyle] = {
    # Heavier, longer rests, little conditioning: the work that drives growth.
    Goal.BUILD: GoalStyle(
        compound=Prescription(4, "6-10", 120),
        isolation=Prescription(3, "10-12", 75),
        conditioning_minutes=0,
        summary="strength and size: heavier sets, full rests",
    ),
    # Moderate loads, shorter rests, conditioning at the end of the session.
    Goal.CUT: GoalStyle(
        compound=Prescription(3, "10-12", 60),
        isolation=Prescription(3, "12-15", 45),
        conditioning_minutes=12,
        summary="lean out: moderate loads, short rests, a conditioning finisher",
    ),
    Goal.MAINTAIN: GoalStyle(
        compound=Prescription(3, "8-12", 90),
        isolation=Prescription(3, "10-15", 60),
        conditioning_minutes=8,
        summary="hold your shape: balanced sets with light conditioning",
    ),
}

# How many movements a session carries before session length is applied.
EXERCISES_BY_EXPERIENCE: dict[TrainingExperience, int] = {
    TrainingExperience.BEGINNER: 5,
    TrainingExperience.INTERMEDIATE: 6,
    TrainingExperience.ADVANCED: 7,
}

# A movement plus its rest costs roughly this long. Used to fit the session
# into the time the client actually has, which is the difference between a
# plan they finish and a plan they abandon halfway.
MINUTES_PER_EXERCISE = 8
MIN_EXERCISES_PER_DAY = 3


# =============================================================================
# Equipment: a hard filter, never a suggestion
# =============================================================================

# Always available, wherever someone trains: their own body, and the cheap
# things that come with a mat.
ALWAYS_AVAILABLE: frozenset[Equipment] = frozenset(
    {Equipment.BODYWEIGHT, Equipment.STRETCH, Equipment.FOAM_ROLLER}
)

# A home gym with nothing declared. Deliberately narrow — a plan that assumes
# equipment someone does not own is worse than no plan.
HOME_DEFAULT: frozenset[Equipment] = ALWAYS_AVAILABLE | frozenset(
    {Equipment.BAND, Equipment.YOGA, Equipment.PILATES}
)


def allowed_equipment(
    location: TrainingLocation, declared: list[str] | None
) -> frozenset[Equipment]:
    """What this client can actually train with."""
    chosen = set()
    for raw in declared or []:
        try:
            chosen.add(Equipment(raw))
        except ValueError:
            continue  # An unknown value from an older client build: ignore it.

    if chosen:
        return frozenset(chosen) | ALWAYS_AVAILABLE
    if location is TrainingLocation.HOME:
        return HOME_DEFAULT
    return frozenset(Equipment)  # A commercial gym: everything is on the floor.


# =============================================================================
# Splits: which muscles on which day
# =============================================================================


@dataclass(frozen=True, slots=True)
class Slot:
    """One position in a session: what it trains and what kind of lift it is."""

    groups: tuple[MuscleGroup, ...]
    mechanics: Mechanics


@dataclass(frozen=True, slots=True)
class DayTemplate:
    label: str
    focus: str
    slots: tuple[Slot, ...]


C = Mechanics.COMPOUND
I = Mechanics.ISOLATION  # noqa: E741 — reads as "isolation" in the tables below

_PUSH = DayTemplate(
    "Day A", "Push — chest, shoulders, triceps",
    (
        Slot((MuscleGroup.CHEST,), C),
        Slot((MuscleGroup.SHOULDERS,), C),
        Slot((MuscleGroup.CHEST,), I),
        Slot((MuscleGroup.TRICEPS,), I),
        Slot((MuscleGroup.SHOULDERS,), I),
        Slot((MuscleGroup.TRICEPS,), I),
        Slot((MuscleGroup.ABS,), I),
    ),
)
_PULL = DayTemplate(
    "Day B", "Pull — back and biceps",
    (
        Slot((MuscleGroup.LATS, MuscleGroup.UPPER_BACK), C),
        Slot((MuscleGroup.UPPER_BACK, MuscleGroup.LATS), C),
        Slot((MuscleGroup.LATS,), I),
        Slot((MuscleGroup.BICEPS,), I),
        Slot((MuscleGroup.TRAPS,), I),
        Slot((MuscleGroup.BICEPS,), I),
        Slot((MuscleGroup.LOWER_BACK,), I),
    ),
)
_LEGS = DayTemplate(
    "Day C", "Legs and core",
    (
        Slot((MuscleGroup.QUADS,), C),
        Slot((MuscleGroup.HAMSTRINGS, MuscleGroup.GLUTES), C),
        Slot((MuscleGroup.GLUTES,), I),
        Slot((MuscleGroup.QUADS,), I),
        Slot((MuscleGroup.HAMSTRINGS,), I),
        Slot((MuscleGroup.CALVES,), I),
        Slot((MuscleGroup.ABS,), I),
    ),
)
_UPPER = DayTemplate(
    "Upper", "Upper body",
    (
        Slot((MuscleGroup.CHEST,), C),
        Slot((MuscleGroup.LATS, MuscleGroup.UPPER_BACK), C),
        Slot((MuscleGroup.SHOULDERS,), C),
        Slot((MuscleGroup.BICEPS,), I),
        Slot((MuscleGroup.TRICEPS,), I),
        Slot((MuscleGroup.UPPER_BACK,), I),
        Slot((MuscleGroup.ABS,), I),
    ),
)
_LOWER = DayTemplate(
    "Lower", "Lower body",
    (
        Slot((MuscleGroup.QUADS,), C),
        Slot((MuscleGroup.HAMSTRINGS, MuscleGroup.GLUTES), C),
        Slot((MuscleGroup.GLUTES,), I),
        Slot((MuscleGroup.QUADS,), I),
        Slot((MuscleGroup.CALVES,), I),
        Slot((MuscleGroup.ABS,), I),
        Slot((MuscleGroup.OBLIQUES,), I),
    ),
)
_FULL_A = DayTemplate(
    "Day A", "Full body — squat and push",
    (
        Slot((MuscleGroup.QUADS,), C),
        Slot((MuscleGroup.CHEST,), C),
        Slot((MuscleGroup.LATS, MuscleGroup.UPPER_BACK), C),
        Slot((MuscleGroup.SHOULDERS,), I),
        Slot((MuscleGroup.ABS,), I),
        Slot((MuscleGroup.CALVES,), I),
    ),
)
_FULL_B = DayTemplate(
    "Day B", "Full body — hinge and pull",
    (
        Slot((MuscleGroup.HAMSTRINGS, MuscleGroup.GLUTES), C),
        Slot((MuscleGroup.UPPER_BACK, MuscleGroup.LATS), C),
        Slot((MuscleGroup.SHOULDERS,), C),
        Slot((MuscleGroup.BICEPS,), I),
        Slot((MuscleGroup.TRICEPS,), I),
        Slot((MuscleGroup.OBLIQUES,), I),
    ),
)
_FULL_C = DayTemplate(
    "Day C", "Full body — lunge and carry",
    (
        Slot((MuscleGroup.GLUTES,), C),
        Slot((MuscleGroup.CHEST,), C),
        Slot((MuscleGroup.LATS,), C),
        Slot((MuscleGroup.HAMSTRINGS,), I),
        Slot((MuscleGroup.ABS,), I),
        Slot((MuscleGroup.FOREARMS,), I),
    ),
)


@dataclass(frozen=True, slots=True)
class Split:
    name: str
    days: tuple[DayTemplate, ...]
    weekdays: tuple[int, ...]  # 0 = Monday


def choose_split(days_per_week: int, experience: TrainingExperience) -> Split:
    """
    The split that matches the week the client can actually commit to.

    Full body for two or three days (every muscle gets two or three exposures
    a week, which is what drives progress at that frequency), upper/lower at
    four, push/pull/legs once there are five or six sessions to fill. A
    beginner stays on full body at three days: fewer movements, repeated more
    often, is how technique is learned.
    """
    days = max(2, min(6, days_per_week))

    if days == 2:
        return Split("Full body", (_FULL_A, _FULL_B), (0, 3))
    if days == 3:
        if experience is TrainingExperience.BEGINNER:
            return Split("Full body", (_FULL_A, _FULL_B, _FULL_C), (0, 2, 4))
        return Split("Push / Pull / Legs", (_PUSH, _PULL, _LEGS), (0, 2, 4))
    if days == 4:
        return Split(
            "Upper / Lower",
            (
                _UPPER,
                _LOWER,
                DayTemplate("Upper B", "Upper body — volume", _UPPER.slots[::-1]),
                DayTemplate("Lower B", "Lower body — volume", _LOWER.slots[::-1]),
            ),
            (0, 1, 3, 4),
        )
    if days == 5:
        return Split(
            "Push / Pull / Legs + Upper / Lower",
            (_PUSH, _PULL, _LEGS, _UPPER, _LOWER),
            (0, 1, 2, 4, 5),
        )
    return Split(
        "Push / Pull / Legs ×2",
        (
            _PUSH,
            _PULL,
            _LEGS,
            DayTemplate("Day D", "Push — volume", _PUSH.slots[::-1]),
            DayTemplate("Day E", "Pull — volume", _PULL.slots[::-1]),
            DayTemplate("Day F", "Legs — volume", _LEGS.slots[::-1]),
        ),
        (0, 1, 2, 3, 4, 5),
    )


# =============================================================================
# Reading the intake
# =============================================================================


@dataclass(slots=True)
class PlanInputs:
    goal: Goal
    location: TrainingLocation
    experience: TrainingExperience
    equipment: frozenset[Equipment]
    days_per_week: int
    session_minutes: int
    level: TrainingLevel
    bmi: float | None
    assumptions: list[str]


def _bmi(profile: ClientProfile) -> float | None:
    height_cm = float(profile.height_cm) if profile.height_cm else None
    weight_kg = (
        float(profile.current_weight_kg)
        if profile.current_weight_kg
        else float(profile.starting_weight_kg)
        if profile.starting_weight_kg
        else None
    )
    if not height_cm or not weight_kg or height_cm < 90:
        return None
    return round(weight_kg / (height_cm / 100) ** 2, 1)


def read_inputs(profile: ClientProfile, level: TrainingLevel | None) -> PlanInputs:
    """Turn a profile into everything the builder needs, or say what is missing."""
    if profile is None:
        raise WorkoutPlanInputError("This client has no profile yet.")
    if not profile.height_cm or not (profile.current_weight_kg or profile.starting_weight_kg):
        raise WorkoutPlanInputError(
            "Height and weight are needed before a training plan can be built."
        )
    if not profile.training_location:
        raise WorkoutPlanInputError("The training intake has not been completed yet.")

    assumptions: list[str] = []

    try:
        location = TrainingLocation(profile.training_location)
    except ValueError:
        location = TrainingLocation.GYM
        assumptions.append("a gym as the training place")

    try:
        experience = TrainingExperience(profile.training_experience or "")
    except ValueError:
        experience = TrainingExperience.BEGINNER
        assumptions.append("a beginner's training history")

    days = profile.weekly_workout_target or 3
    session_minutes = profile.session_minutes or 45

    return PlanInputs(
        goal=profile.goal or Goal.MAINTAIN,
        location=location,
        experience=experience,
        equipment=allowed_equipment(location, profile.available_equipment),
        days_per_week=max(2, min(6, days)),
        session_minutes=max(20, min(120, session_minutes)),
        level=level or profile.level or TrainingLevel.LEVEL_1,
        bmi=_bmi(profile),
        assumptions=assumptions,
    )


def exercises_per_day(inputs: PlanInputs) -> int:
    """As many movements as fit the session the client says they have."""
    by_experience = EXERCISES_BY_EXPERIENCE[inputs.experience]
    by_clock = inputs.session_minutes // MINUTES_PER_EXERCISE
    return max(MIN_EXERCISES_PER_DAY, min(by_experience, by_clock))


# =============================================================================
# Choosing the movements
# =============================================================================


async def _candidate_pool(
    db: AsyncSession, inputs: PlanInputs
) -> dict[tuple[MuscleGroup, Mechanics], list[Exercise]]:
    """
    Every usable movement, bucketed by (muscle group, compound/isolation).

    One query for the whole plan. Ordered by popularity so the block opens with
    movements a client has heard of, which matters more for adherence than any
    amount of cleverness in the selection below.
    """
    rows = (
        (
            await db.execute(
                select(Exercise)
                .where(
                    Exercise.is_active.is_(True),
                    Exercise.equipment.in_(inputs.equipment),
                    Exercise.min_level.in_(_levels_up_to(inputs.level)),
                )
                .order_by(Exercise.popularity.desc(), Exercise.name)
            )
        )
        .scalars()
        .all()
    )

    # The same rule the coach's plan builder enforces: no movement without a
    # demonstration. Anything unwatchable is dropped here rather than shipped.
    index = await build_video_index(db, {row.id for row in rows})
    usable = [row for row in rows if row.video_url or index.get(row.id)]

    pool: dict[tuple[MuscleGroup, Mechanics], list[Exercise]] = {}
    for exercise in usable:
        mechanics = exercise.mechanics or Mechanics.ISOLATION
        pool.setdefault((exercise.muscle_group, mechanics), []).append(exercise)
    return pool


def _levels_up_to(level: TrainingLevel) -> list[TrainingLevel]:
    order = [TrainingLevel.LEVEL_1, TrainingLevel.LEVEL_2, TrainingLevel.LEVEL_3]
    return order[: order.index(level) + 1]


def _pick(
    pool: dict[tuple[MuscleGroup, Mechanics], list[Exercise]],
    slot: Slot,
    used_in_plan: set[uuid.UUID],
    used_today: set[uuid.UUID],
) -> Exercise | None:
    """
    The best movement for a slot, under two rules in order of importance.

    Never the same movement twice in one session — a client reading "Romanian
    Deadlift" at positions two and five loses trust in the whole plan. Across
    different days it is fine and often correct: a bench press belongs in both
    a push day and an upper day.

    Within those rules it falls back through the exact muscle group and
    mechanics, then the slot's secondary groups, then the other mechanics for
    the same muscle — a small home-equipment library runs out of exact matches
    quickly, and training the muscle a second way beats leaving a gap. If
    nothing at all fits, the slot is dropped rather than padded.
    """
    keys = [(group, slot.mechanics) for group in slot.groups]
    keys += [(group, _other(slot.mechanics)) for group in slot.groups]

    # First choice: something this plan has not used anywhere yet.
    for key in keys:
        for exercise in pool.get(key, ()):
            if exercise.id not in used_in_plan:
                return exercise
    # Second: anything not already in today's session.
    for key in keys:
        for exercise in pool.get(key, ()):
            if exercise.id not in used_today:
                return exercise
    return None


def _other(mechanics: Mechanics) -> Mechanics:
    return Mechanics.ISOLATION if mechanics is Mechanics.COMPOUND else Mechanics.COMPOUND


# =============================================================================
# Building and storing the plan
# =============================================================================


def _notes(inputs: PlanInputs, split: Split) -> str:
    style = GOAL_STYLES[inputs.goal]
    where = {
        TrainingLocation.GYM: "at the gym",
        TrainingLocation.HOME: "at home",
        TrainingLocation.HYBRID: "at home and at the gym",
    }[inputs.location]

    lines = [
        f"Built from your intake: {split.name.lower()}, {inputs.days_per_week} days a week "
        f"{where}, about {inputs.session_minutes} minutes a session — {style.summary}.",
        "Add a little weight or one more rep whenever you finish every set at the top of "
        "the range. Every movement links to a demonstration video.",
    ]
    if style.conditioning_minutes:
        lines.append(
            f"Finish each session with {style.conditioning_minutes} minutes of steady "
            "cardio, logged under Sleep & Cardio."
        )
    if inputs.bmi and inputs.bmi >= 30:
        lines.append(
            "Your first block favours machines, bodyweight and controlled tempo over "
            "heavy free-weight loading, to build the base with less joint stress."
        )
    if inputs.assumptions:
        lines.append(
            "Assumed " + "; ".join(inputs.assumptions) + " — update your intake for a closer fit."
        )
    lines.append("Your coach reviews this and adjusts it at your check-ins.")
    return " ".join(lines)


async def generate_workout_plan(
    db: AsyncSession,
    client_id: uuid.UUID,
    *,
    level: TrainingLevel | None = None,
    assigned_by_id: uuid.UUID | None = None,
    activate: bool = True,
) -> WorkoutPlan:
    """Build and store an automatic block. Raises `WorkoutPlanInputError`."""
    profile = (
        (await db.execute(select(ClientProfile).where(ClientProfile.user_id == client_id)))
        .scalars()
        .first()
    )
    inputs = read_inputs(profile, level)
    style = GOAL_STYLES[inputs.goal]
    split = choose_split(inputs.days_per_week, inputs.experience)
    per_day = exercises_per_day(inputs)

    pool = await _candidate_pool(db, inputs)
    if not pool:
        raise WorkoutPlanInputError(
            "No movements in the library match that equipment yet. "
            "Add equipment in your intake, or ask your coach to assign a plan."
        )

    plan = WorkoutPlan(
        client_id=client_id,
        assigned_by_id=assigned_by_id,
        name=f"Auto plan · {split.name} · {inputs.days_per_week}× week",
        level=inputs.level,
        week_number=1,
        total_weeks=8,
        notes=_notes(inputs, split),
        is_custom=False,
        is_active=activate,
        source=SOURCE_AUTO,
    )
    db.add(plan)
    await db.flush()

    used_in_plan: set[uuid.UUID] = set()
    for order, template in enumerate(split.days):
        day = WorkoutDay(
            plan_id=plan.id,
            label=template.label,
            focus=template.focus,
            day_of_week=split.weekdays[order] if order < len(split.weekdays) else None,
            order_index=order,
            estimated_minutes=inputs.session_minutes,
        )
        db.add(day)
        await db.flush()

        position = 0
        used_today: set[uuid.UUID] = set()
        for slot in template.slots[:per_day]:
            exercise = _pick(pool, slot, used_in_plan, used_today)
            if exercise is None:
                continue
            used_in_plan.add(exercise.id)
            used_today.add(exercise.id)
            prescription = (
                style.compound if slot.mechanics is Mechanics.COMPOUND else style.isolation
            )
            db.add(
                WorkoutDayExercise(
                    day_id=day.id,
                    exercise_id=exercise.id,
                    order_index=position,
                    sets=prescription.sets,
                    rep_range=prescription.rep_range,
                    rest_seconds=prescription.rest_seconds,
                    coach_note=exercise.coaching_cue,
                )
            )
            position += 1

    if activate:
        await db.execute(
            update(WorkoutPlan)
            .where(WorkoutPlan.client_id == client_id, WorkoutPlan.id != plan.id)
            .values(is_active=False)
        )

    # Keep the profile in step with the plan the client just received, so the
    # dashboard's weekly targets describe the training they were actually given.
    profile.weekly_workout_target = inputs.days_per_week
    if style.conditioning_minutes:
        profile.weekly_cardio_target_min = style.conditioning_minutes * inputs.days_per_week
    if profile.program_start_date is None:
        profile.program_start_date = date.today()

    await db.flush()

    plan = (
        await db.execute(
            select(WorkoutPlan)
            .where(WorkoutPlan.id == plan.id)
            .options(
                selectinload(WorkoutPlan.days)
                .selectinload(WorkoutDay.exercises)
                .selectinload(WorkoutDayExercise.exercise)
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    log.info(
        "training.auto_plan_generated",
        client_id=str(client_id),
        plan_id=str(plan.id),
        split=split.name,
        days=inputs.days_per_week,
        movements=sum(len(day.exercises) for day in plan.days),
    )
    return plan


async def active_plan(db: AsyncSession, client_id: uuid.UUID) -> WorkoutPlan | None:
    return (
        await db.execute(
            select(WorkoutPlan)
            .where(WorkoutPlan.client_id == client_id, WorkoutPlan.is_active.is_(True))
            .order_by(WorkoutPlan.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def ensure_auto_workout_plan(
    db: AsyncSession, client_id: uuid.UUID, *, level: TrainingLevel | None = None
) -> WorkoutPlan | None:
    """
    Give a client a block if they have none — the automatic half of the feature.

    Called from billing (a subscription starting) and from the intake form,
    which is why it never raises: a missing height must not fail a Stripe
    webhook. An incomplete intake is a normal outcome, logged and skipped; the
    portal asks the client for it and calls back here the moment it arrives.
    """
    if not settings.AUTO_WORKOUT_PLAN_ENABLED:
        return None
    try:
        if await active_plan(db, client_id) is not None:
            return None
        async with db.begin_nested():
            return await generate_workout_plan(db, client_id, level=level)
    except WorkoutPlanInputError as exc:
        log.info("training.auto_plan_skipped", client_id=str(client_id), reason=str(exc))
    except Exception as exc:  # noqa: BLE001 — must never break billing
        log.error("training.auto_plan_failed", client_id=str(client_id), error=str(exc))
    return None