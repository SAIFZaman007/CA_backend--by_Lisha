"""Writing programmes: training blocks and meal plans.

Both are edited as a whole document rather than row by row. The coach drags a
day around, changes three rep ranges and presses save once — so the write path
replaces the nested structure inside a single transaction. Half-saved plans are
not a thing a client should ever be able to open.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update

from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.models.catalog import Exercise
from app.models.nutrition import Meal, MealItem, MealPlan
from app.models.training import WorkoutDay, WorkoutDayExercise, WorkoutPlan
from app.models.user import User
from app.services.programming import assert_every_movement_has_video
from app.schemas.admin import (
    MealOut,
    MealPlanIn,
    MealPlanOut,
    PlanDayOut,
    PlanExerciseOut,
    WorkoutPlanIn,
    WorkoutPlanOut,
)

router = APIRouter()
log = get_logger("admin.programming")


async def _require_client(db: DbSession, client_id: uuid.UUID) -> User:
    user = await db.get(User, client_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That client was not found.")
    return user


def _plan_out(plan: WorkoutPlan, video_index: dict | None = None) -> WorkoutPlanOut:
    """Serialise a plan, resolving each prescription's demonstration video.

    `video_index` is the map `assert_every_movement_has_video` already built
    during a write; passing it through avoids rebuilding it. On a plain read it
    is absent and the per-prescription override plus the library link on the
    eagerly-loaded `item.exercise` are enough — no extra query either way.
    """
    index = video_index or {}
    return WorkoutPlanOut(
        id=plan.id,
        name=plan.name,
        level=plan.level,
        week_number=plan.week_number,
        total_weeks=plan.total_weeks,
        notes=plan.notes,
        is_custom=plan.is_custom,
        is_active=plan.is_active,
        created_at=plan.created_at,
        days=[
            PlanDayOut(
                id=day.id,
                label=day.label,
                focus=day.focus,
                day_of_week=day.day_of_week,
                order_index=day.order_index,
                estimated_minutes=day.estimated_minutes,
                exercises=[
                    PlanExerciseOut(
                        id=item.id,
                        exercise_id=item.exercise_id,
                        exercise_name=item.exercise.name if item.exercise else "Removed movement",
                        target_muscle=item.exercise.target_muscle if item.exercise else "—",
                        order_index=item.order_index,
                        sets=item.sets,
                        rep_range=item.rep_range,
                        rest_seconds=item.rest_seconds,
                        tempo=item.tempo,
                        target_weight_kg=float(item.target_weight_kg)
                        if item.target_weight_kg is not None
                        else None,
                        coach_note=item.coach_note,
                        video_url=(
                            item.video_url
                            or (item.exercise.video_url if item.exercise else None)
                            or index.get(item.exercise_id)
                        ),
                    )
                    for item in day.exercises
                ],
            )
            for day in plan.days
        ],
    )


async def _write_days(db: DbSession, plan: WorkoutPlan, payload: WorkoutPlanIn) -> None:
    """Rebuild the day/exercise tree under a plan. Every referenced movement is
    verified first, so a bad ID fails the whole save rather than silently
    dropping an exercise out of the client's programme."""
    # The rule a client's programme depends on: every prescribed movement must
    # resolve to a demonstration they can watch. Enforced here rather than in
    # the dashboard, because the API is the boundary — a plan written by a
    # script or a replayed request would walk straight past a front-end check.
    prescriptions = [
        (item.exercise_id, item.video_url) for day in payload.days for item in day.exercises
    ]
    if prescriptions:
        await assert_every_movement_has_video(db, prescriptions)

    wanted = {item.exercise_id for day in payload.days for item in day.exercises}
    if wanted:
        found = set(
            (await db.execute(select(Exercise.id).where(Exercise.id.in_(wanted))))
            .scalars()
            .all()
        )
        missing = wanted - found
        if missing:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="One of the movements in this plan no longer exists. Refresh and retry.",
            )

    for day in list(plan.days):
        await db.delete(day)
    await db.flush()

    for day_index, day_in in enumerate(payload.days):
        day = WorkoutDay(
            plan_id=plan.id,
            label=day_in.label,
            focus=day_in.focus,
            day_of_week=day_in.day_of_week,
            order_index=day_index,
            estimated_minutes=day_in.estimated_minutes,
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
                    tempo=item.tempo,
                    target_weight_kg=item.target_weight_kg,
                    coach_note=item.coach_note,
                    video_url=str(item.video_url) if item.video_url else None,
                )
            )
    await db.flush()


async def _deactivate_other_plans(
    db: DbSession, client_id: uuid.UUID, keep_id: uuid.UUID
) -> None:
    """Exactly one training block is live at a time — that is what the portal reads."""
    await db.execute(
        update(WorkoutPlan)
        .where(WorkoutPlan.client_id == client_id, WorkoutPlan.id != keep_id)
        .values(is_active=False)
    )


# --- Training plans -----------------------------------------------------------


@router.get("/clients/{client_id}/plans", response_model=list[WorkoutPlanOut])
async def list_plans(
    client_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> list[WorkoutPlanOut]:
    await _require_client(db, client_id)
    plans = (
        (
            await db.execute(
                select(WorkoutPlan)
                .where(WorkoutPlan.client_id == client_id)
                .order_by(WorkoutPlan.is_active.desc(), WorkoutPlan.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_plan_out(plan) for plan in plans]


@router.post(
    "/clients/{client_id}/plans",
    response_model=WorkoutPlanOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_plan(
    client_id: uuid.UUID, payload: WorkoutPlanIn, coach: CurrentCoach, db: DbSession
) -> WorkoutPlanOut:
    await _require_client(db, client_id)

    plan = WorkoutPlan(
        client_id=client_id,
        assigned_by_id=coach.id,
        program_id=payload.program_id,
        name=payload.name,
        level=payload.level,
        week_number=payload.week_number,
        total_weeks=payload.total_weeks,
        notes=payload.notes,
        is_custom=False,
        is_active=payload.is_active,
    )
    db.add(plan)
    await db.flush()

    await _write_days(db, plan, payload)
    if payload.is_active:
        await _deactivate_other_plans(db, client_id, plan.id)

    await db.refresh(plan, ["days"])
    log.info("admin.plan_created", client_id=str(client_id), plan_id=str(plan.id))
    return _plan_out(plan)


@router.put("/plans/{plan_id}", response_model=WorkoutPlanOut)
async def replace_plan(
    plan_id: uuid.UUID, payload: WorkoutPlanIn, coach: CurrentCoach, db: DbSession
) -> WorkoutPlanOut:
    plan = await db.get(WorkoutPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    plan.name = payload.name
    plan.level = payload.level
    plan.week_number = payload.week_number
    plan.total_weeks = payload.total_weeks
    plan.notes = payload.notes
    plan.is_active = payload.is_active
    plan.program_id = payload.program_id
    plan.assigned_by_id = coach.id

    await _write_days(db, plan, payload)
    if payload.is_active:
        await _deactivate_other_plans(db, plan.client_id, plan.id)

    await db.refresh(plan, ["days"])
    log.info("admin.plan_replaced", plan_id=str(plan_id))
    return _plan_out(plan)


@router.post("/plans/{plan_id}/activate", response_model=WorkoutPlanOut)
async def activate_plan(
    plan_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> WorkoutPlanOut:
    plan = await db.get(WorkoutPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    plan.is_active = True
    await _deactivate_other_plans(db, plan.client_id, plan.id)
    await db.flush()
    return _plan_out(plan)


@router.post("/plans/{plan_id}/duplicate", response_model=WorkoutPlanOut)
async def duplicate_plan(
    plan_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> WorkoutPlanOut:
    """Next week's block usually starts as last week's block. This is how a coach
    progresses someone without retyping fourteen exercises."""
    source = await db.get(WorkoutPlan, plan_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    copy = WorkoutPlan(
        client_id=source.client_id,
        assigned_by_id=coach.id,
        program_id=source.program_id,
        name=f"{source.name} (week {source.week_number + 1})",
        level=source.level,
        week_number=min(source.week_number + 1, source.total_weeks),
        total_weeks=source.total_weeks,
        notes=source.notes,
        is_custom=False,
        is_active=False,
    )
    db.add(copy)
    await db.flush()

    for day in source.days:
        new_day = WorkoutDay(
            plan_id=copy.id,
            label=day.label,
            focus=day.focus,
            day_of_week=day.day_of_week,
            order_index=day.order_index,
            estimated_minutes=day.estimated_minutes,
        )
        db.add(new_day)
        await db.flush()
        for item in day.exercises:
            db.add(
                WorkoutDayExercise(
                    day_id=new_day.id,
                    exercise_id=item.exercise_id,
                    order_index=item.order_index,
                    sets=item.sets,
                    rep_range=item.rep_range,
                    rest_seconds=item.rest_seconds,
                    tempo=item.tempo,
                    target_weight_kg=item.target_weight_kg,
                    coach_note=item.coach_note,
                    video_url=item.video_url,
                )
            )

    await db.flush()
    await db.refresh(copy, ["days"])
    return _plan_out(copy)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: uuid.UUID, coach: CurrentCoach, db: DbSession) -> None:
    plan = await db.get(WorkoutPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")
    await db.delete(plan)
    log.info("admin.plan_deleted", plan_id=str(plan_id))


# --- Meal plans ---------------------------------------------------------------


def _meal_plan_out(plan: MealPlan) -> MealPlanOut:
    return MealPlanOut(
        id=plan.id,
        name=plan.name,
        phase=plan.phase,
        calorie_target=plan.calorie_target,
        protein_target_g=plan.protein_target_g,
        carb_target_g=plan.carb_target_g,
        fat_target_g=plan.fat_target_g,
        notes=plan.notes,
        is_active=plan.is_active,
        created_at=plan.created_at,
        meals=[
            MealOut(
                id=meal.id,
                day_of_week=meal.day_of_week,
                order_index=meal.order_index,
                name=meal.name,
                serve_time=meal.serve_time,
                icon=meal.icon,
                calories=meal.calories,
                protein_g=meal.protein_g,
                carbs_g=meal.carbs_g,
                fat_g=meal.fat_g,
                notes=meal.notes,
                items=[item.label for item in meal.items],
            )
            for meal in plan.meals
        ],
    )


async def _write_meals(db: DbSession, plan: MealPlan, payload: MealPlanIn) -> None:
    for meal in list(plan.meals):
        await db.delete(meal)
    await db.flush()

    per_day: dict[int, int] = {}
    for meal_in in payload.meals:
        order = per_day.get(meal_in.day_of_week, 0)
        per_day[meal_in.day_of_week] = order + 1

        meal = Meal(
            plan_id=plan.id,
            day_of_week=meal_in.day_of_week,
            order_index=order,
            name=meal_in.name,
            serve_time=meal_in.serve_time,
            icon=meal_in.icon,
            calories=meal_in.calories,
            protein_g=meal_in.protein_g,
            carbs_g=meal_in.carbs_g,
            fat_g=meal_in.fat_g,
            notes=meal_in.notes,
        )
        db.add(meal)
        await db.flush()

        for item_order, label in enumerate(meal_in.items):
            cleaned = label.strip()
            if cleaned:
                db.add(MealItem(meal_id=meal.id, label=cleaned[:160], order_index=item_order))
    await db.flush()


@router.get("/clients/{client_id}/meal-plans", response_model=list[MealPlanOut])
async def list_meal_plans(
    client_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> list[MealPlanOut]:
    await _require_client(db, client_id)
    plans = (
        (
            await db.execute(
                select(MealPlan)
                .where(MealPlan.client_id == client_id)
                .order_by(MealPlan.is_active.desc(), MealPlan.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_meal_plan_out(plan) for plan in plans]


@router.post(
    "/clients/{client_id}/meal-plans",
    response_model=MealPlanOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_meal_plan(
    client_id: uuid.UUID, payload: MealPlanIn, coach: CurrentCoach, db: DbSession
) -> MealPlanOut:
    await _require_client(db, client_id)

    plan = MealPlan(
        client_id=client_id,
        assigned_by_id=coach.id,
        name=payload.name,
        phase=payload.phase,
        calorie_target=payload.calorie_target,
        protein_target_g=payload.protein_target_g,
        carb_target_g=payload.carb_target_g,
        fat_target_g=payload.fat_target_g,
        notes=payload.notes,
        is_active=payload.is_active,
    )
    db.add(plan)
    await db.flush()

    await _write_meals(db, plan, payload)
    if payload.is_active:
        await db.execute(
            update(MealPlan)
            .where(MealPlan.client_id == client_id, MealPlan.id != plan.id)
            .values(is_active=False)
        )

    await db.refresh(plan, ["meals"])
    log.info("admin.meal_plan_created", client_id=str(client_id), plan_id=str(plan.id))
    return _meal_plan_out(plan)


@router.put("/meal-plans/{plan_id}", response_model=MealPlanOut)
async def replace_meal_plan(
    plan_id: uuid.UUID, payload: MealPlanIn, coach: CurrentCoach, db: DbSession
) -> MealPlanOut:
    plan = await db.get(MealPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That meal plan was not found.")

    plan.name = payload.name
    plan.phase = payload.phase
    plan.calorie_target = payload.calorie_target
    plan.protein_target_g = payload.protein_target_g
    plan.carb_target_g = payload.carb_target_g
    plan.fat_target_g = payload.fat_target_g
    plan.notes = payload.notes
    plan.is_active = payload.is_active
    plan.assigned_by_id = coach.id

    await _write_meals(db, plan, payload)
    if payload.is_active:
        await db.execute(
            update(MealPlan)
            .where(MealPlan.client_id == plan.client_id, MealPlan.id != plan.id)
            .values(is_active=False)
        )

    await db.refresh(plan, ["meals"])
    return _meal_plan_out(plan)


@router.post("/meal-plans/{plan_id}/activate", response_model=MealPlanOut)
async def activate_meal_plan(
    plan_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> MealPlanOut:
    plan = await db.get(MealPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That meal plan was not found.")

    plan.is_active = True
    await db.execute(
        update(MealPlan)
        .where(MealPlan.client_id == plan.client_id, MealPlan.id != plan.id)
        .values(is_active=False)
    )
    await db.flush()
    return _meal_plan_out(plan)


@router.delete("/meal-plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meal_plan(plan_id: uuid.UUID, coach: CurrentCoach, db: DbSession) -> None:
    plan = await db.get(MealPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That meal plan was not found.")
    await db.delete(plan)