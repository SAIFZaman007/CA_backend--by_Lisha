"""The training-block rule that cannot live in the UI.

A client must never open their programme and find a movement they have no way
to see performed. Enforcing that in the dashboard alone would be theatre: the
API is the boundary, the dashboard is one caller of it, and a plan written by a
script, a future mobile client or a replayed request would walk straight past a
front-end check.

So the rule lives here, and `admin/programming.py` calls it before any write.

A prescription resolves to a video in one of three ways, in order:

1. `WorkoutDayExercise.video_url` — the coach's own clip for *this* client's
   version of the movement. Highest priority because it is the most specific.
2. `Exercise.video_url` — the library demonstration. Where nearly everything
   resolves.
3. A published `VideoTutorial` linked to that exercise — the in-house recording
   from the tutorial library.

If none of the three produces a URL, the save is refused, naming the exact
movements at fault so the coach can fix them rather than hunt for them.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Exercise
from app.models.media import VideoTutorial


@dataclass(frozen=True)
class DemoVideo:
    """Where a client's "how do I do this" link points, and what produced it."""

    url: str
    source: str  # "prescription" | "library" | "tutorial"


async def build_video_index(
    db: AsyncSession, exercise_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Best available demonstration per exercise, library or tutorial.

    Two queries for the whole plan rather than two per movement. A fourteen-day
    block with ten movements a day would otherwise be 280 round trips on a
    single save, which is how a plan editor ends up taking eight seconds.
    """
    if not exercise_ids:
        return {}

    index: dict[uuid.UUID, str] = {}

    tutorial_rows = (
        await db.execute(
            select(VideoTutorial.exercise_id, VideoTutorial.video_url)
            .where(
                VideoTutorial.exercise_id.in_(exercise_ids),
                VideoTutorial.is_published.is_(True),
                VideoTutorial.video_url.is_not(None),
            )
            .order_by(VideoTutorial.is_featured.desc(), VideoTutorial.sort_order)
        )
    ).all()
    for exercise_id, url in tutorial_rows:
        if exercise_id and url:
            index.setdefault(exercise_id, url)

    # The library link wins over a tutorial: it is the canonical demonstration
    # of the movement itself, where a tutorial may be a wider lesson that only
    # mentions it. `setdefault` above leaves tutorials as the fallback.
    library_rows = (
        await db.execute(
            select(Exercise.id, Exercise.video_url).where(
                Exercise.id.in_(exercise_ids), Exercise.video_url.is_not(None)
            )
        )
    ).all()
    for exercise_id, url in library_rows:
        if url:
            index[exercise_id] = url

    return index


def resolve_demo_video(
    *,
    prescription_url: str | None,
    exercise_id: uuid.UUID,
    index: dict[uuid.UUID, str],
) -> DemoVideo | None:
    """Apply the three-step resolution for one prescription."""
    if prescription_url:
        return DemoVideo(url=prescription_url, source="prescription")

    fallback = index.get(exercise_id)
    if fallback:
        return DemoVideo(url=fallback, source="library")
    return None


async def assert_every_movement_has_video(
    db: AsyncSession,
    prescriptions: list[tuple[uuid.UUID, str | None]],
) -> dict[uuid.UUID, str]:
    """Refuse the save unless every prescription resolves to a video.

    `prescriptions` is (exercise_id, per-prescription override URL) for every
    movement in the block. Returns the resolved index so the caller does not
    repeat the work.

    The error names the offending movements. "One of your exercises has no
    video" sends a coach through fourteen days looking for it; naming them is
    the difference between a fixable message and a frustrating one.
    """
    exercise_ids = {exercise_id for exercise_id, _ in prescriptions}
    index = await build_video_index(db, exercise_ids)

    missing_ids = {
        exercise_id
        for exercise_id, override in prescriptions
        if resolve_demo_video(
            prescription_url=override, exercise_id=exercise_id, index=index
        )
        is None
    }
    if not missing_ids:
        return index

    names = (
        (
            await db.execute(
                select(Exercise.name).where(Exercise.id.in_(missing_ids)).order_by(Exercise.name)
            )
        )
        .scalars()
        .all()
    )
    listed = ", ".join(names) if names else "one of the selected movements"
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"Every movement needs a demonstration video before this block goes out. "
            f"Missing: {listed}. Add a link on the movement in Exercise Library, "
            f"or paste one against the exercise in this block."
        ),
    )