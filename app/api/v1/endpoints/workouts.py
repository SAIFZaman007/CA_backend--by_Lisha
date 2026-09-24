"""Training: the intake, the plan built from it, and what the client lifted."""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentUser, DbSession
from app.models.enums import (
    EQUIPMENT_LABELS,
    Equipment,
    SessionStatus,
    TrainingLevel,
)
from app.models.training import (
    SetLog,
    WorkoutDay,
    WorkoutDayExercise,
    WorkoutPlan,
    WorkoutSession,
)
from app.models.user import ClientProfile
from app.schemas.training import (
    CustomPlanIn,
    EquipmentOption,
    IntakeFormOut,
    IntakeIn,
    IntakeOut,
    IntakeResultOut,
    SessionStart,
    SessionUpdate,
    SetLogIn,
    WorkoutPlanOut,
    WorkoutSessionOut,
)
from app.services import workout_planner
from app.services.workout_planner import SOURCE_AUTO, WorkoutPlanInputError

router = APIRouter(prefix="/workouts", tags=["workouts"])


async def _load_active_plan(db: DbSession, client_id: uuid.UUID) -> WorkoutPlan | None:
    stmt = (
        select(WorkoutPlan)
        .where(WorkoutPlan.client_id == client_id, WorkoutPlan.is_active.is_(True))
        .options(
            selectinload(WorkoutPlan.days)
            .selectinload(WorkoutDay.exercises)
            .selectinload(WorkoutDayExercise.exercise)
        )
        .order_by(WorkoutPlan.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().first()


async def _owned_day(db: DbSession, day_id: uuid.UUID, client_id: uuid.UUID) -> WorkoutDay:
    day = (
        await db.execute(
            select(WorkoutDay)
            .join(WorkoutPlan)
            .where(WorkoutDay.id == day_id, WorkoutPlan.client_id == client_id)
        )
    ).scalar_one_or_none()
    if day is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That training day is not yours.")
    return day


# --- Intake and the automatic plan ---------------------------------------------

async def _profile(db: DbSession, user: CurrentUser) -> ClientProfile:
    profile = (
        (await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id)))
        .scalars()
        .first()
    )
    if profile is None:
        profile = ClientProfile(user_id=user.id)
        db.add(profile)
        await db.flush()
    return profile


def _intake_out(profile: ClientProfile) -> IntakeOut:
    return IntakeOut(
        is_complete=profile.intake_completed_at is not None,
        completed_at=profile.intake_completed_at,
        height_cm=float(profile.height_cm) if profile.height_cm else None,
        current_weight_kg=(
            float(profile.current_weight_kg) if profile.current_weight_kg else None
        ),
        goal_weight_kg=float(profile.goal_weight_kg) if profile.goal_weight_kg else None,
        date_of_birth=profile.date_of_birth,
        sex=profile.sex,
        unit_system=profile.unit_system,
        goal=profile.goal,
        training_location=profile.training_location,
        training_experience=profile.training_experience,
        available_equipment=list(profile.available_equipment or []),
        days_per_week=profile.weekly_workout_target,
        session_minutes=profile.session_minutes,
        medical_notes=profile.medical_notes,
    )


# Equipment a client can plausibly own or find, in the order the form shows it.
# Stretching and recovery kit is left out: it is assumed everywhere, and a
# checklist of twenty-four items is a form nobody finishes.
INTAKE_EQUIPMENT: tuple[Equipment, ...] = (
    Equipment.BODYWEIGHT,
    Equipment.DUMBBELL,
    Equipment.BARBELL,
    Equipment.KETTLEBELL,
    Equipment.BAND,
    Equipment.MACHINE,
    Equipment.CABLE,
    Equipment.SMITH_MACHINE,
    Equipment.EZ_BAR,
    Equipment.TRAP_BAR,
    Equipment.LANDMINE,
    Equipment.SUSPENSION,
    Equipment.MEDICINE_BALL,
    Equipment.EXERCISE_BALL,
    Equipment.PLYO_BOX,
    Equipment.WEIGHT_PLATE,
    Equipment.CARDIO_MACHINE,
)


@router.get("/intake", response_model=IntakeFormOut)
async def get_intake(user: CurrentUser, db: DbSession) -> IntakeFormOut:
    """The client's saved answers and the options the form offers."""
    profile = await _profile(db, user)
    plan = await workout_planner.active_plan(db, user.id)
    return IntakeFormOut(
        intake=_intake_out(profile),
        equipment_options=[
            EquipmentOption(value=item, label=EQUIPMENT_LABELS[item]) for item in INTAKE_EQUIPMENT
        ],
        has_plan=plan is not None,
        plan_source=plan.source if plan else None,
    )


@router.put("/intake", response_model=IntakeResultOut)
async def save_intake(payload: IntakeIn, user: CurrentUser, db: DbSession) -> IntakeResultOut:
    """
    Save the intake — and hand back a training plan built from it.

    One request, because that is the promise the form makes: fill this in and
    your programme is there. Splitting it into "save" then "generate" would
    leave a client staring at an empty Workout tab if the second call failed.

    Idempotent: answering it again updates the profile and rebuilds the block,
    unless the coach has since written one of their own — theirs wins, and the
    client is told so rather than silently losing it.
    """
    profile = await _profile(db, user)

    profile.height_cm = payload.height_cm
    profile.current_weight_kg = payload.current_weight_kg
    if profile.starting_weight_kg is None:
        profile.starting_weight_kg = payload.current_weight_kg
    if payload.goal_weight_kg is not None:
        profile.goal_weight_kg = payload.goal_weight_kg
    if payload.date_of_birth is not None:
        profile.date_of_birth = payload.date_of_birth
    if payload.sex is not None:
        profile.sex = payload.sex
    if payload.unit_system is not None:
        profile.unit_system = payload.unit_system

    profile.goal = payload.goal
    profile.training_location = payload.training_location.value
    profile.training_experience = payload.training_experience.value
    profile.available_equipment = [item.value for item in payload.available_equipment]
    profile.weekly_workout_target = payload.days_per_week
    profile.session_minutes = payload.session_minutes
    profile.medical_notes = payload.medical_notes
    profile.onboarding_completed = True
    profile.intake_completed_at = datetime.now(UTC)
    await db.flush()

    plan, message = await _rebuild_plan(db, user)
    return IntakeResultOut(intake=_intake_out(profile), plan=plan, message=message)


@router.post("/plan/generate", response_model=IntakeResultOut)
async def regenerate_plan(user: CurrentUser, db: DbSession) -> IntakeResultOut:
    """
    Rebuild the automatic block — after a weight change, or for a fresh block.

    Refuses when the active plan is the coach's: replacing prescribed training
    with a generated block is not a decision a button should make.
    """
    profile = await _profile(db, user)
    if profile.intake_completed_at is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Complete your training intake first — it is what the plan is built from.",
        )
    plan, message = await _rebuild_plan(db, user)
    return IntakeResultOut(intake=_intake_out(profile), plan=plan, message=message)


async def _rebuild_plan(db: DbSession, user: CurrentUser) -> tuple[WorkoutPlan | None, str | None]:
    """Build a fresh automatic plan, leaving a coach-written one untouched."""
    current = await workout_planner.active_plan(db, user.id)
    if current is not None and current.source != SOURCE_AUTO:
        return (
            await _load_plan_by_id(db, current.id),
            "Your coach has written your current plan, so it has been kept. "
            "Message them if you would like it rebuilt around your new answers.",
        )

    level = user.profile.level if user.profile else None
    try:
        plan = await workout_planner.generate_workout_plan(db, user.id, level=level)
    except WorkoutPlanInputError as exc:
        return None, str(exc)
    return plan, None


@router.get("/plan", response_model=WorkoutPlanOut | None)
async def active_plan(user: CurrentUser, db: DbSession) -> WorkoutPlan | None:
    """The plan the client trains from today. Null until a coach assigns one."""
    return await _load_active_plan(db, user.id)


@router.get("/plans", response_model=list[WorkoutPlanOut])
async def list_plans(user: CurrentUser, db: DbSession) -> list[WorkoutPlan]:
    stmt = (
        select(WorkoutPlan)
        .where(WorkoutPlan.client_id == user.id)
        .options(
            selectinload(WorkoutPlan.days)
            .selectinload(WorkoutDay.exercises)
            .selectinload(WorkoutDayExercise.exercise)
        )
        .order_by(WorkoutPlan.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


@router.post("/plans/custom", response_model=WorkoutPlanOut, status_code=status.HTTP_201_CREATED)
async def create_custom_plan(
    payload: CustomPlanIn, user: CurrentUser, db: DbSession
) -> WorkoutPlan:
    """Clients can write their own sessions from the exercise library.

    Custom plans sit alongside the coach's plan rather than replacing it, so a
    prescribed block is never overwritten by accident.
    """
    level = user.profile.level if user.profile else TrainingLevel.LEVEL_1
    plan = WorkoutPlan(
        client_id=user.id,
        name=payload.name.strip(),
        level=level,
        notes=payload.notes,
        is_custom=True,
        is_active=False,
        week_number=1,
        total_weeks=1,
    )
    db.add(plan)
    await db.flush()

    for day_index, day_in in enumerate(payload.days):
        day = WorkoutDay(
            plan_id=plan.id,
            label=day_in.label,
            focus=day_in.focus,
            day_of_week=day_in.day_of_week,
            order_index=day_index,
        )
        db.add(day)
        await db.flush()
        for order, item in enumerate(day_in.exercises):
            db.add(
                WorkoutDayExercise(
                    day_id=day.id,
                    exercise_id=item.exercise_id,
                    order_index=order,
                    sets=item.sets,
                    rep_range=item.rep_range,
                    rest_seconds=item.rest_seconds,
                    coach_note=item.coach_note,
                )
            )

    await db.flush()
    return await _load_plan_by_id(db, plan.id)


async def _load_plan_by_id(db: DbSession, plan_id: uuid.UUID) -> WorkoutPlan:
    stmt = (
        select(WorkoutPlan)
        .where(WorkoutPlan.id == plan_id)
        .options(
            selectinload(WorkoutPlan.days)
            .selectinload(WorkoutDay.exercises)
            .selectinload(WorkoutDayExercise.exercise)
        )
    )
    return (await db.execute(stmt)).scalar_one()


@router.post("/plans/{plan_id}/activate", response_model=WorkoutPlanOut)
async def activate_plan(plan_id: uuid.UUID, user: CurrentUser, db: DbSession) -> WorkoutPlan:
    plan = (
        await db.execute(
            select(WorkoutPlan).where(
                WorkoutPlan.id == plan_id, WorkoutPlan.client_id == user.id
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan is not yours.")

    for other in (
        await db.execute(
            select(WorkoutPlan).where(
                WorkoutPlan.client_id == user.id, WorkoutPlan.id != plan_id
            )
        )
    ).scalars():
        other.is_active = False
    plan.is_active = True
    await db.flush()
    return await _load_plan_by_id(db, plan.id)


# --- Sessions ------------------------------------------------------------------


async def _owned_session(db: DbSession, session_id: uuid.UUID, client_id: uuid.UUID) -> WorkoutSession:
    session = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == session_id, WorkoutSession.client_id == client_id)
            .options(selectinload(WorkoutSession.sets))
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That session is not yours.")
    return session


@router.post("/sessions", response_model=WorkoutSessionOut, status_code=status.HTTP_201_CREATED)
async def start_session(
    payload: SessionStart, user: CurrentUser, db: DbSession
) -> WorkoutSession:
    """Begin a training day. Re-opening the same day returns the existing session
    rather than creating a duplicate."""
    await _owned_day(db, payload.day_id, user.id)
    on_date = payload.session_date or date.today()

    existing = (
        await db.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.client_id == user.id,
                WorkoutSession.day_id == payload.day_id,
                WorkoutSession.session_date == on_date,
            )
            .options(selectinload(WorkoutSession.sets))
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    session = WorkoutSession(
        client_id=user.id,
        day_id=payload.day_id,
        session_date=on_date,
        status=SessionStatus.IN_PROGRESS,
    )
    db.add(session)
    await db.flush()
    # Re-read with `sets` eagerly loaded — serialising a freshly flushed object
    # would otherwise trigger a lazy load outside the async greenlet context.
    return await _owned_session(db, session.id, user.id)


@router.get("/sessions", response_model=list[WorkoutSessionOut])
async def list_sessions(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(30, ge=1, le=365),
) -> list[WorkoutSession]:
    since = date.today() - timedelta(days=days)
    stmt = (
        select(WorkoutSession)
        .where(WorkoutSession.client_id == user.id, WorkoutSession.session_date >= since)
        .options(selectinload(WorkoutSession.sets))
        .order_by(WorkoutSession.session_date.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


@router.patch("/sessions/{session_id}", response_model=WorkoutSessionOut)
async def update_session(
    session_id: uuid.UUID, payload: SessionUpdate, user: CurrentUser, db: DbSession
) -> WorkoutSession:
    session = await _owned_session(db, session_id, user.id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(session, field, value)
    if data.get("status") == SessionStatus.COMPLETED and session.completed_at is None:
        session.completed_at = datetime.now(UTC)
    await db.flush()
    return session


@router.put("/sessions/{session_id}/sets", response_model=WorkoutSessionOut)
async def log_set(
    session_id: uuid.UUID, payload: SetLogIn, user: CurrentUser, db: DbSession
) -> WorkoutSession:
    """Record one set. Sending the same exercise and set number again overwrites
    it, so a mistyped weight is fixed by simply re-entering it."""
    session = await _owned_session(db, session_id, user.id)

    belongs = (
        await db.execute(
            select(WorkoutDayExercise.id).where(
                WorkoutDayExercise.id == payload.day_exercise_id,
                WorkoutDayExercise.day_id == session.day_id,
            )
        )
    ).scalar_one_or_none()
    if belongs is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="That exercise is not part of this session."
        )

    existing = (
        await db.execute(
            select(SetLog).where(
                and_(
                    SetLog.session_id == session.id,
                    SetLog.day_exercise_id == payload.day_exercise_id,
                    SetLog.set_number == payload.set_number,
                )
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.weight_kg = payload.weight_kg
        existing.reps = payload.reps
        existing.rpe = payload.rpe
        existing.is_completed = payload.is_completed
    else:
        db.add(SetLog(session_id=session.id, **payload.model_dump()))

    await db.flush()
    return await _owned_session(db, session_id, user.id)


@router.delete("/sessions/{session_id}/sets/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_set(
    session_id: uuid.UUID, set_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    session = await _owned_session(db, session_id, user.id)
    row = (
        await db.execute(
            select(SetLog).where(SetLog.id == set_id, SetLog.session_id == session.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That set was not found.")
    await db.delete(row)


@router.get("/history/{exercise_id}")
async def exercise_history(
    exercise_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = Query(10, ge=1, le=50)
) -> list[dict]:
    """Last N sessions for one movement — so the client can see what to beat."""
    stmt = (
        select(
            WorkoutSession.session_date,
            func.max(SetLog.weight_kg).label("top_weight"),
            func.sum(SetLog.weight_kg * SetLog.reps).label("volume"),
            func.count(SetLog.id).label("sets"),
        )
        .join(SetLog, SetLog.session_id == WorkoutSession.id)
        .join(WorkoutDayExercise, WorkoutDayExercise.id == SetLog.day_exercise_id)
        .where(
            WorkoutSession.client_id == user.id,
            WorkoutDayExercise.exercise_id == exercise_id,
            SetLog.is_completed.is_(True),
        )
        .group_by(WorkoutSession.session_date)
        .order_by(WorkoutSession.session_date.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "date": row.session_date.isoformat(),
            "top_weight_kg": float(row.top_weight) if row.top_weight else None,
            "volume_kg": float(row.volume) if row.volume else 0,
            "sets": row.sets,
        }
        for row in rows
    ]