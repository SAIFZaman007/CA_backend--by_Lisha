"""One call that fills the client's home screen.

Rolled up server-side so the portal makes a single request on load rather than
six, which keeps the dashboard fast on a phone.
"""

from datetime import date, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.models.engagement import Message, MessageThread
from app.models.enums import SessionStatus
from app.models.nutrition import Meal, MealLog, MealPlan
from app.models.tracking import CardioLog, SleepLog, WeightLog
from app.models.training import WorkoutDay, WorkoutPlan, WorkoutSession
from app.schemas.tracking import DashboardOut, DashboardTrend

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


@router.get("", response_model=DashboardOut)
async def dashboard(user: CurrentUser, db: DbSession) -> DashboardOut:
    today = date.today()
    monday = _week_start(today)
    profile = user.profile

    # --- Weight -------------------------------------------------------------
    weights = (
        await db.execute(
            select(WeightLog)
            .where(WeightLog.client_id == user.id)
            .order_by(WeightLog.log_date.desc())
            .limit(12)
        )
    ).scalars().all()
    weights = list(reversed(weights))

    current_weight = float(weights[-1].weight_kg) if weights else None
    start_weight = (
        float(profile.starting_weight_kg)
        if profile and profile.starting_weight_kg
        else (float(weights[0].weight_kg) if weights else None)
    )
    weight_change = (
        round(current_weight - start_weight, 2)
        if current_weight is not None and start_weight is not None
        else None
    )

    # --- Training this week -------------------------------------------------
    workouts_done = (
        await db.execute(
            select(func.count(WorkoutSession.id)).where(
                WorkoutSession.client_id == user.id,
                WorkoutSession.session_date >= monday,
                WorkoutSession.status == SessionStatus.COMPLETED,
            )
        )
    ).scalar_one()

    # Consecutive weeks in which the weekly target was met.
    target = profile.weekly_workout_target if profile else 3
    streak = 0
    for weeks_back in range(0, 52):
        start = monday - timedelta(weeks=weeks_back)
        end = start + timedelta(days=6)
        done = (
            await db.execute(
                select(func.count(WorkoutSession.id)).where(
                    WorkoutSession.client_id == user.id,
                    WorkoutSession.session_date >= start,
                    WorkoutSession.session_date <= end,
                    WorkoutSession.status == SessionStatus.COMPLETED,
                )
            )
        ).scalar_one()
        if done >= target:
            streak += 1
        elif weeks_back > 0:
            break

    # --- Today's training day ----------------------------------------------
    today_day_id = (
        await db.execute(
            select(WorkoutDay.id)
            .join(WorkoutPlan)
            .where(
                WorkoutPlan.client_id == user.id,
                WorkoutPlan.is_active.is_(True),
                WorkoutDay.day_of_week == today.weekday(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    # --- Calories eaten today ----------------------------------------------
    calories_today = (
        await db.execute(
            select(func.coalesce(func.sum(Meal.calories), 0))
            .select_from(MealLog)
            .join(Meal, Meal.id == MealLog.meal_id)
            .join(MealPlan, MealPlan.id == Meal.plan_id)
            .where(
                MealLog.client_id == user.id,
                MealLog.log_date == today,
                MealLog.is_completed.is_(True),
                MealPlan.is_active.is_(True),
            )
        )
    ).scalar_one()

    # --- Sleep and cardio ---------------------------------------------------
    last_night = (
        await db.execute(
            select(SleepLog.hours_slept)
            .where(SleepLog.client_id == user.id)
            .order_by(SleepLog.log_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    cardio_minutes = (
        await db.execute(
            select(func.coalesce(func.sum(CardioLog.duration_minutes), 0)).where(
                CardioLog.client_id == user.id, CardioLog.log_date >= monday
            )
        )
    ).scalar_one()

    unread = (
        await db.execute(
            select(func.count(Message.id))
            .join(MessageThread)
            .where(
                MessageThread.client_id == user.id,
                Message.sender_id != user.id,
                Message.read_at.is_(None),
            )
        )
    ).scalar_one()

    return DashboardOut(
        greeting_name=(user.display_name or user.full_name).split()[0],
        level=profile.level if profile else "level_1",
        phase=profile.phase if profile else None,
        program_week=profile.program_week if profile else 1,
        program_total_weeks=profile.program_total_weeks if profile else 12,
        current_weight_kg=current_weight,
        weight_change_kg=weight_change,
        calories_today=int(calories_today or 0),
        calorie_target=profile.calorie_target if profile else None,
        workouts_done_this_week=workouts_done,
        weekly_workout_target=target,
        weekly_streak=streak,
        sleep_last_night_hours=float(last_night) if last_night is not None else None,
        cardio_minutes_this_week=int(cardio_minutes or 0),
        cardio_target_minutes=profile.weekly_cardio_target_min if profile else 150,
        weight_trend=[
            DashboardTrend(label=w.log_date.isoformat(), value=float(w.weight_kg))
            for w in weights
        ],
        today_day_id=today_day_id,
        unread_messages=unread,
    )
