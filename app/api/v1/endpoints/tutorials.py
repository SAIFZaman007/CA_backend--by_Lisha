"""Video tutorials — the read-only library inside the client portal.

Clients see published recordings only. Everything here is filtered by
`is_published`, so an unfinished draft in the dashboard never leaks.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, OptionalUser
from app.core.security import sign_media_url, verify_media_token
from app.models.enums import Equipment, TutorialCategory
from app.models.media import VideoTutorial
from app.schemas.media import TutorialFilters, TutorialOut
from app.services import storage

router = APIRouter(prefix="/tutorials", tags=["tutorials"])


@router.get("", response_model=list[TutorialOut])
async def list_tutorials(
    user: CurrentUser,
    db: DbSession,
    search: str | None = Query(None, max_length=80),
    category: TutorialCategory | None = None,
    target_muscle: str | None = Query(None, max_length=80),
    equipment: Equipment | None = None,
    exercise_id: uuid.UUID | None = None,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[VideoTutorial]:
    stmt = select(VideoTutorial).where(VideoTutorial.is_published.is_(True))

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                VideoTutorial.title.ilike(pattern),
                VideoTutorial.summary.ilike(pattern),
                VideoTutorial.target_muscle.ilike(pattern),
            )
        )
    if category:
        stmt = stmt.where(VideoTutorial.category == category)
    if target_muscle:
        stmt = stmt.where(VideoTutorial.target_muscle.ilike(target_muscle))
    if equipment:
        stmt = stmt.where(VideoTutorial.equipment == equipment)
    if exercise_id:
        stmt = stmt.where(VideoTutorial.exercise_id == exercise_id)

    stmt = (
        stmt.order_by(
            VideoTutorial.is_featured.desc(),
            VideoTutorial.sort_order,
            VideoTutorial.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [attach_stream_url(row, user.id) for row in rows]


@router.get("/filters", response_model=TutorialFilters)
async def tutorial_filters(user: CurrentUser, db: DbSession) -> TutorialFilters:
    """Only offers filters that would actually return something."""
    published = VideoTutorial.is_published.is_(True)

    categories = (
        (
            await db.execute(
                select(VideoTutorial.category).where(published).distinct()
            )
        )
        .scalars()
        .all()
    )
    muscles = (
        (
            await db.execute(
                select(VideoTutorial.target_muscle)
                .where(published, VideoTutorial.target_muscle.is_not(None))
                .distinct()
                .order_by(VideoTutorial.target_muscle)
            )
        )
        .scalars()
        .all()
    )
    kit = (
        (
            await db.execute(
                select(VideoTutorial.equipment)
                .where(published, VideoTutorial.equipment.is_not(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    order = [c.value for c in TutorialCategory]
    return TutorialFilters(
        categories=sorted((c.value for c in categories), key=order.index),
        target_muscles=list(muscles),
        equipment=sorted(e.value for e in kit),
    )


@router.get("/{tutorial_id}", response_model=TutorialOut)
async def get_tutorial(
    tutorial_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> VideoTutorial:
    tutorial = await db.get(VideoTutorial, tutorial_id)
    if tutorial is None or not tutorial.is_published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That tutorial was not found.")
    return attach_stream_url(tutorial, user.id)


@router.post("/{tutorial_id}/view", status_code=status.HTTP_204_NO_CONTENT)
async def record_view(tutorial_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    """Counts a play so the coach can see which clips clients actually use.

    Incremented in SQL rather than read-modify-write, so two clients pressing
    play at the same moment cannot lose a count.
    """
    tutorial = await db.get(VideoTutorial, tutorial_id)
    if tutorial is None or not tutorial.is_published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That tutorial was not found.")

    await db.execute(
        VideoTutorial.__table__.update()
        .where(VideoTutorial.id == tutorial_id)
        .values(view_count=func.coalesce(VideoTutorial.view_count, 0) + 1)
    )

def attach_stream_url(tutorial: VideoTutorial, viewer_id: uuid.UUID) -> VideoTutorial:
    """Give an uploaded tutorial a playable address.

    A hosted tutorial already has one. An uploaded one is a private file, so it
    gets a signed URL that a <video> tag can load directly — the same trick the
    check-in photos use, and for the same reason: media elements cannot send an
    Authorization header. Deliberately a longer-lived signature than a photo's
    (`MEDIA_VIDEO_URL_TTL_SECONDS`, not `MEDIA_URL_TTL_SECONDS`) — see the
    comment on that setting for why a video needs more runway than an image.
    """
    if tutorial.file_key and not tutorial.video_url:
        token = sign_media_url(tutorial.id, viewer_id, settings.MEDIA_VIDEO_URL_TTL_SECONDS)
        tutorial.video_url = f"/api/v1/tutorials/{tutorial.id}/stream?token={token}"
    return tutorial


@router.get("/{tutorial_id}/stream")
async def stream_tutorial(
    tutorial_id: uuid.UUID,
    db: DbSession,
    user: OptionalUser = None,
    token: str | None = Query(None),
) -> FileResponse:
    """Serve an uploaded tutorial file.

    Accepts a bearer token or a signed `token` parameter, so both a scripted
    fetch and a plain <video src> work.
    """
    viewer_id = user.id if user else (verify_media_token(token, tutorial_id) if token else None)
    if viewer_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="This video link has expired. Reload the page."
        )

    tutorial = await db.get(VideoTutorial, tutorial_id)
    if tutorial is None or not tutorial.is_published or not tutorial.file_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That tutorial was not found.")

    return FileResponse(
        storage.resolve_path(tutorial.file_key, not_found_message="That video could not be found."),
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=900", "Accept-Ranges": "bytes"},
    )