"""Training: read the assigned plan, build your own, log what you lifted."""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentUser, DbSession
from app.models.enums import SessionStatus, TrainingLevel
from app.models.training import (
    SetLog,
    WorkoutDay,
    WorkoutDayExercise,
    WorkoutPlan,
    WorkoutSession,
)
from app.schemas.training import (
    CustomPlanIn,
    SessionStart,
    SessionUpdate,
    SetLogIn,
    WorkoutPlanOut,
    WorkoutSessionOut,
)

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
