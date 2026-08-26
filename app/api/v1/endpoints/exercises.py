"""Exercise library — browsable by every signed-in client, editable by the coach.

Each movement carries a video link so a client never has to guess at form. The
two browse axes the coach's picker offers, muscle group and equipment, are both
enum-backed columns rather than free text, so a filter can never miss a
movement to a spelling difference.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from slugify import slugify
from sqlalchemy import func, or_, select

from app.core.deps import CurrentCoach, CurrentUser, DbSession
from app.models.catalog import Exercise
from app.models.enums import Equipment, Mechanics, MuscleGroup
from app.schemas.catalog import (
    ExerciseCreate,
    ExerciseFacets,
    ExerciseOut,
    ExerciseUpdate,
    equipment_options,
    muscle_group_options,
)

router = APIRouter(prefix="/exercises", tags=["exercises"])


@router.get("", response_model=list[ExerciseOut])
async def list_exercises(
    user: CurrentUser,
    db: DbSession,
    search: str | None = Query(None, max_length=80),
    muscle_group: MuscleGroup | None = None,
    equipment: Equipment | None = None,
    mechanics: Mechanics | None = None,
    # `target_muscle` is the old free-text filter. Kept because the client
    # portal still links to it from a workout card, and breaking a URL a client
    # may have bookmarked to save one query parameter is a poor trade.
    target_muscle: str | None = Query(None, max_length=80),
    has_video: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> list[Exercise]:
    stmt = select(Exercise).where(Exercise.is_active.is_(True))

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(Exercise.name.ilike(pattern), Exercise.target_muscle.ilike(pattern))
        )
    if muscle_group:
        stmt = stmt.where(Exercise.muscle_group == muscle_group)
    if target_muscle:
        stmt = stmt.where(Exercise.target_muscle.ilike(target_muscle))
    if equipment:
        stmt = stmt.where(Exercise.equipment == equipment)
    if mechanics:
        stmt = stmt.where(Exercise.mechanics == mechanics)
    if has_video is True:
        stmt = stmt.where(Exercise.video_url.is_not(None))
    elif has_video is False:
        stmt = stmt.where(Exercise.video_url.is_(None))

    # Most-used first, then alphabetical. An unfiltered picker opening on
    # "Ab Wheel Rollout" makes the coach scroll past sixty rarities to reach a
    # squat; opening on what they actually prescribe does not.
    stmt = (
        stmt.order_by(Exercise.popularity.desc(), Exercise.name).limit(limit).offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.get("/filters", response_model=ExerciseFacets)
async def exercise_filters(user: CurrentUser, db: DbSession) -> ExerciseFacets:
    """Both browse axes, with counts, in one request.

    The counts are the point. A coach opening "Palmar Fascia" expecting a
    library and finding four movements has been misled by the heading; showing
    the number next to it prevents the click that lands nowhere. Four aggregate
    queries beat twenty-two count queries fired from the client.
    """
    group_counts = dict(
        (
            await db.execute(
                select(Exercise.muscle_group, func.count(Exercise.id))
                .where(Exercise.is_active.is_(True))
                .group_by(Exercise.muscle_group)
            )
        ).all()
    )
    equipment_counts = dict(
        (
            await db.execute(
                select(Exercise.equipment, func.count(Exercise.id))
                .where(Exercise.is_active.is_(True))
                .group_by(Exercise.equipment)
            )
        ).all()
    )
    muscles = (
        (
            await db.execute(
                select(Exercise.target_muscle)
                .where(Exercise.is_active.is_(True))
                .distinct()
                .order_by(Exercise.target_muscle)
            )
        )
        .scalars()
        .all()
    )
    without_video = (
        await db.execute(
            select(func.count(Exercise.id)).where(
                Exercise.is_active.is_(True), Exercise.video_url.is_(None)
            )
        )
    ).scalar_one()

    return ExerciseFacets(
        muscle_groups=muscle_group_options(
            {key.value: value for key, value in group_counts.items()}
        ),
        equipment=equipment_options(
            {key.value: value for key, value in equipment_counts.items()}
        ),
        target_muscles=list(muscles),
        total=sum(group_counts.values()),
        # Surfaced so the dashboard can warn before a plan save fails: any
        # movement counted here will be refused by
        # `assert_every_movement_has_video`.
        without_video=without_video,
    )


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
    for field in ("video_url", "thumbnail_url", "source_url"):
        if data.get(field) is not None:
            data[field] = str(data[field])

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
    for field in ("video_url", "thumbnail_url", "source_url"):
        if field in updates and updates[field] is not None:
            updates[field] = str(updates[field])
    if "name" in updates and updates["name"]:
        exercise.slug = slugify(updates["name"])

    for field, value in updates.items():
        setattr(exercise, field, value)
    await db.flush()
    return exercise


@router.delete("/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_exercise(exercise_id: uuid.UUID, coach: CurrentCoach, db: DbSession) -> None:
    """Soft delete. Historic set logs still reference the movement, so the row stays."""
    exercise = await db.get(Exercise, exercise_id)
    if exercise is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That exercise was not found.")
    exercise.is_active = False