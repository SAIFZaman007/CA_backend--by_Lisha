"""Meal plan for the week, and tick-box adherence logging."""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentUser, DbSession
from app.core.logging import get_logger
from app.models.enums import UserRole
from app.models.nutrition import Meal, MealLog, MealPlan
from app.schemas.tracking import MealLogIn, MealLogOut, MealOut, MealPlanOut
from app.services import entitlements
from app.services.meal_planner import SOURCE_AUTO, MealPlanInputError, generate_meal_plan

router = APIRouter(prefix="/nutrition", tags=["nutrition"])
log = get_logger("nutrition")


async def _active_plan(db: DbSession, client_id: uuid.UUID) -> MealPlan | None:
    stmt = (
        select(MealPlan)
        .where(MealPlan.client_id == client_id, MealPlan.is_active.is_(True))
        .options(selectinload(MealPlan.meals).selectinload(Meal.items))
        .order_by(MealPlan.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().first()


@router.get("/plan", response_model=MealPlanOut | None)
async def active_meal_plan(user: CurrentUser, db: DbSession) -> MealPlan | None:
    """The full week. Null until a coach assigns a plan."""
    return await _active_plan(db, user.id)


@router.post("/plan/generate", response_model=MealPlanOut, status_code=status.HTTP_201_CREATED)
async def generate_my_meal_plan(user: CurrentUser, db: DbSession) -> MealPlan:
    """Build (or refresh) the client's automatic plan from their latest numbers.

    Allowed when the client has no active plan, or when the active plan is an
    automatic one — e.g. after logging a new weight. A plan the coach wrote or
    edited is never replaced from here; that stays the coach's call.
    """
    if user.role is not UserRole.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only clients have meal plans.")

    entitlement = await entitlements.entitlement_for(db, user)
    if not entitlement.has("meal_plan"):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail="Choose a coaching plan to unlock your meal plan.",
        )

    current = await _active_plan(db, user.id)
    if current is not None and current.source != SOURCE_AUTO:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Your coach has written your current plan. Message them to change it.",
        )

    try:
        plan = await generate_meal_plan(db, user.id)
    except MealPlanInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    log.info("nutrition.self_generated", user_id=str(user.id), plan_id=str(plan.id))
    return plan


@router.get("/plan/day/{day_of_week}", response_model=list[MealOut])
async def meals_for_day(
    day_of_week: int, user: CurrentUser, db: DbSession
) -> list[Meal]:
    """0 = Monday through 6 = Sunday."""
    if not 0 <= day_of_week <= 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Day must be between 0 and 6.")

    plan = await _active_plan(db, user.id)
    if plan is None:
        return []
    return [meal for meal in plan.meals if meal.day_of_week == day_of_week]


@router.get("/logs", response_model=list[MealLogOut])
async def list_meal_logs(
    user: CurrentUser,
    db: DbSession,
    on_date: date | None = Query(None, description="Defaults to today"),
) -> list[MealLog]:
    target = on_date or date.today()
    stmt = select(MealLog).where(
        MealLog.client_id == user.id, MealLog.log_date == target
    )
    return list((await db.execute(stmt)).scalars().all())


@router.put("/logs", response_model=MealLogOut)
async def upsert_meal_log(
    payload: MealLogIn, user: CurrentUser, db: DbSession
) -> MealLog:
    """Tick or untick a meal. Idempotent — one row per meal per day."""
    target = payload.log_date or date.today()

    owned = (
        await db.execute(
            select(Meal.id)
            .join(MealPlan)
            .where(Meal.id == payload.meal_id, MealPlan.client_id == user.id)
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That meal is not on your plan.")

    existing = (
        await db.execute(
            select(MealLog).where(
                MealLog.client_id == user.id,
                MealLog.meal_id == payload.meal_id,
                MealLog.log_date == target,
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.is_completed = payload.is_completed
        existing.actual_calories = payload.actual_calories
        await db.flush()
        return existing

    log_row = MealLog(
        client_id=user.id,
        meal_id=payload.meal_id,
        log_date=target,
        is_completed=payload.is_completed,
        actual_calories=payload.actual_calories,
    )
    db.add(log_row)
    await db.flush()
    return log_row


@router.get("/today")
async def today_totals(user: CurrentUser, db: DbSession) -> dict:
    """What has actually been eaten today against target."""
    today = date.today()
    plan = await _active_plan(db, user.id)
    if plan is None:
        return {
            "calories_eaten": 0,
            "calorie_target": user.profile.calorie_target if user.profile else None,
            "protein_g": 0,
            "carbs_g": 0,
            "fat_g": 0,
            "meals_completed": 0,
            "meals_total": 0,
        }

    todays_meals = [m for m in plan.meals if m.day_of_week == today.weekday()]
    meal_ids = {m.id for m in todays_meals}

    logs = (
        await db.execute(
            select(MealLog).where(
                MealLog.client_id == user.id,
                MealLog.log_date == today,
                MealLog.is_completed.is_(True),
            )
        )
    ).scalars().all()
    done_ids = {log.meal_id for log in logs} & meal_ids
    eaten = [m for m in todays_meals if m.id in done_ids]

    return {
        "calories_eaten": sum(m.calories for m in eaten),
        "calorie_target": plan.calorie_target,
        "protein_g": sum(m.protein_g for m in eaten),
        "protein_target_g": plan.protein_target_g,
        "carbs_g": sum(m.carbs_g for m in eaten),
        "carb_target_g": plan.carb_target_g,
        "fat_g": sum(m.fat_g for m in eaten),
        "fat_target_g": plan.fat_target_g,
        "meals_completed": len(eaten),
        "meals_total": len(todays_meals),
    }