"""Exercise library — browsable by every signed-in client, editable by the coach.

Each movement carries a video link so a client never has to guess at form.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from slugify import slugify
from sqlalchemy import or_, select

from app.core.deps import CurrentCoach, CurrentUser, DbSession
from app.models.catalog import Exercise
from app.models.enums import Equipment
from app.schemas.catalog import ExerciseCreate, ExerciseOut, ExerciseUpdate

router = APIRouter(prefix="/exercises", tags=["exercises"])


@router.get("", response_model=list[ExerciseOut])
async def list_exercises(
    user: CurrentUser,
    db: DbSession,
    search: str | None = Query(None, max_length=80),
    target_muscle: str | None = Query(None, max_length=80),
    equipment: Equipment | None = None,
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> list[Exercise]:
    stmt = select(Exercise).where(Exercise.is_active.is_(True))

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(Exercise.name.ilike(pattern), Exercise.target_muscle.ilike(pattern))
        )
    if target_muscle:
        stmt = stmt.where(Exercise.target_muscle.ilike(target_muscle))
    if equipment:
        stmt = stmt.where(Exercise.equipment == equipment)

    stmt = stmt.order_by(Exercise.name).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars().all())


@router.get("/filters")
async def exercise_filters(user: CurrentUser, db: DbSession) -> dict:
    muscles = (
        await db.execute(
            select(Exercise.target_muscle)
            .where(Exercise.is_active.is_(True))
            .distinct()
            .order_by(Exercise.target_muscle)
        )
    ).scalars().all()
    return {
        "target_muscles": list(muscles),
        "equipment": [e.value for e in Equipment],
    }


@router.get("/{exercise_id}", response_model=ExerciseOut)
async def get_exercise(exercise_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Exercise:
    exercise = await db.get(Exercise, exercise_id)
    if exercise is None or not exercise.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That exercise was not found.")
    return exercise


@router.post("", response_model=ExerciseOut, status_code=status.HTTP_201_CREATED)
async def create_exercise(
    payload: ExerciseCreate, coach: CurrentCoach, db: DbSession
) -> Exercise:
    slug = slugify(payload.name)
    taken = (
        await db.execute(select(Exercise.id).where(Exercise.slug == slug))
    ).scalar_one_or_none()
    if taken:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="An exercise with that name already exists."
        )

    data = payload.model_dump()
    data["video_url"] = str(payload.video_url) if payload.video_url else None
    data["thumbnail_url"] = str(payload.thumbnail_url) if payload.thumbnail_url else None

    exercise = Exercise(slug=slug, created_by_id=coach.id, **data)
    db.add(exercise)
    await db.flush()
    return exercise


@router.patch("/{exercise_id}", response_model=ExerciseOut)
async def update_exercise(
    exercise_id: uuid.UUID, payload: ExerciseUpdate, coach: CurrentCoach, db: DbSession
) -> Exercise:
    exercise = await db.get(Exercise, exercise_id)
    if exercise is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That exercise was not found.")

    updates = payload.model_dump(exclude_unset=True)
    for field in ("video_url", "thumbnail_url"):
        if field in updates and updates[field] is not None:
            updates[field] = str(updates[field])
    if "name" in updates and updates["name"]:
        exercise.slug = slugify(updates["name"])

    for field, value in updates.items():
        setattr(exercise, field, value)
    await db.flush()
    return exercise


@router.delete("/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_exercise(
    exercise_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> None:
    """Soft delete. Historic set logs still reference the movement, so the row stays."""
    exercise = await db.get(Exercise, exercise_id)
    if exercise is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That exercise was not found.")
    exercise.is_active = False
