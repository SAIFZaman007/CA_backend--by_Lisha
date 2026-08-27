"""
What the business sells and what it teaches.

One person runs Coach Auto: they coach and they administer. Splitting pricing
behind a separate admin role meant the only human who works here could not edit
her own prices, so everything in this module is `CurrentCoach` — which admits
both roles and keeps the single-operator case working whichever one the account
happens to carry.

Two catalogues live here: the pricing plans shown on the public site, and the
video tutorial library shown inside the client portal.
"""

import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from slugify import slugify
from sqlalchemy import func, or_, select

from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.models.catalog import Program
from app.models.enums import Equipment, TrainingLevel, TutorialCategory, VideoProvider
from app.models.media import VideoTutorial
from app.models.training import WorkoutPlan
from app.schemas.admin import ProgramAdminOut, ProgramIn, ProgramUpdate
from app.schemas.media import (
    TutorialAdminOut,
    TutorialCreate,
    TutorialUpdate,
    default_thumbnail,
    detect_provider,
)
from app.services import storage

router = APIRouter()
log = get_logger("admin.catalog")


def _image_url(program: Program) -> str | None:
    """Whichever artwork the coach supplied: an upload wins over a pasted link."""
    if program.image_key:
        # See the comment on `upload_program_image`'s return value — the real
        # route has no `/public` segment.
        return f"/api/v1/programs/{program.id}/image"
    return program.image_external_url


async def _unique_slug(db: DbSession, model, base: str, exclude: uuid.UUID | None = None) -> str:
    """Slugs are public URLs, so a collision must not silently overwrite one."""
    root = slugify(base)[:150] or "item"
    candidate = root
    suffix = 2
    while True:
        stmt = select(model.id).where(model.slug == candidate)
        if exclude:
            stmt = stmt.where(model.id != exclude)
        clash = (await db.execute(stmt)).scalar_one_or_none()
        if clash is None:
            return candidate
        candidate = f"{root}-{suffix}"
        suffix += 1


# --- Pricing plans ------------------------------------------------------------


@router.get("/programs", response_model=list[ProgramAdminOut])
async def list_programs(
    coach: CurrentCoach,
    db: DbSession,
    include_archived: bool = Query(True),
) -> list[ProgramAdminOut]:
    counts = (
        select(WorkoutPlan.program_id.label("pid"), func.count(WorkoutPlan.id).label("n"))
        .where(WorkoutPlan.program_id.is_not(None), WorkoutPlan.is_active.is_(True))
        .group_by(WorkoutPlan.program_id)
        .subquery()
    )

    stmt = (
        select(Program, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.pid == Program.id)
        .order_by(Program.sort_order, Program.price_cents)
    )
    if not include_archived:
        stmt = stmt.where(Program.is_active.is_(True))

    rows = (await db.execute(stmt)).all()
    return [
        ProgramAdminOut.model_validate(program).model_copy(
            update={"client_count": count, "image_url": _image_url(program)}
        )
        for program, count in rows
    ]


@router.post("/programs", response_model=ProgramAdminOut, status_code=status.HTTP_201_CREATED)
async def create_program(
    payload: ProgramIn, coach: CurrentCoach, db: DbSession
) -> ProgramAdminOut:
    program = Program(
        slug=await _unique_slug(db, Program, payload.name),
        **payload.model_dump(),
    )
    db.add(program)
    await db.flush()
    await db.refresh(program)
    log.info("admin.program_created", program_id=str(program.id), by=str(coach.id))
    return ProgramAdminOut.model_validate(program).model_copy(
        update={"image_url": _image_url(program)}
    )


@router.patch("/programs/{program_id}", response_model=ProgramAdminOut)
async def update_program(
    program_id: uuid.UUID, payload: ProgramUpdate, coach: CurrentCoach, db: DbSession
) -> ProgramAdminOut:
    program = await db.get(Program, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") and updates["name"] != program.name:
        program.slug = await _unique_slug(db, Program, updates["name"], exclude=program.id)

    for field, value in updates.items():
        setattr(program, field, value)
    await db.flush()
    await db.refresh(program)
    return ProgramAdminOut.model_validate(program).model_copy(
        update={"image_url": _image_url(program)}
    )


@router.delete("/programs/{program_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_program(
    program_id: uuid.UUID,
    coach: CurrentCoach,
    db: DbSession,
    hard: bool = Query(False),
) -> None:
    """Archiving is the default. A plan someone is currently paying for keeps its
    row so their assigned training block still resolves; `hard=true` is refused
    while anybody is on it."""
    program = await db.get(Program, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    in_use = (
        await db.execute(
            select(func.count(WorkoutPlan.id)).where(
                WorkoutPlan.program_id == program_id, WorkoutPlan.is_active.is_(True)
            )
        )
    ).scalar_one()

    if hard:
        if in_use:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"{in_use} client(s) are on this plan. Archive it instead, "
                "or move them across first.",
            )
        await db.delete(program)
        log.warning("admin.program_deleted", program_id=str(program_id), by=str(coach.id))
        return

    program.is_active = False
    program.is_accepting_clients = False
    log.info("admin.program_archived", program_id=str(program_id), by=str(coach.id))


@router.post("/programs/reorder")
async def reorder_programs(
    payload: list[uuid.UUID], coach: CurrentCoach, db: DbSession
) -> dict:
    """Position on the pricing page, in the order the IDs arrive."""
    for index, program_id in enumerate(payload):
        program = await db.get(Program, program_id)
        if program:
            program.sort_order = index
    await db.flush()
    return {"status": "saved"}


# --- Video tutorials ----------------------------------------------------------


@router.get("/tutorials", response_model=list[TutorialAdminOut])
async def list_tutorials(
    coach: CurrentCoach,
    db: DbSession,
    search: str | None = Query(None, max_length=80),
    category: TutorialCategory | None = None,
    equipment: Equipment | None = None,
    min_level: TrainingLevel | None = None,
    published: bool | None = None,
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> list[VideoTutorial]:
    stmt = select(VideoTutorial)

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
    if equipment:
        stmt = stmt.where(VideoTutorial.equipment == equipment)
    if min_level:
        stmt = stmt.where(VideoTutorial.min_level == min_level)
    if published is not None:
        stmt = stmt.where(VideoTutorial.is_published.is_(published))

    stmt = (
        stmt.order_by(
            VideoTutorial.is_featured.desc(),
            VideoTutorial.sort_order,
            VideoTutorial.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.post("/tutorials", response_model=TutorialAdminOut, status_code=status.HTTP_201_CREATED)
async def create_tutorial(
    payload: TutorialCreate, coach: CurrentCoach, db: DbSession
) -> VideoTutorial:
    data = payload.model_dump()
    video_url = str(payload.video_url) if payload.video_url is not None else None

    tutorial = VideoTutorial(
        slug=await _unique_slug(db, VideoTutorial, payload.title),
        created_by_id=coach.id,
        provider=detect_provider(video_url) if video_url is not None else VideoProvider.DIRECT,
        **{
            **data,
            "video_url": video_url,
            "thumbnail_url": str(payload.thumbnail_url) if payload.thumbnail_url else None,
        },
    )
    db.add(tutorial)
    await db.flush()
    await db.refresh(tutorial)
    log.info("admin.tutorial_created", tutorial_id=str(tutorial.id), by=str(coach.id))
    return tutorial


@router.patch("/tutorials/{tutorial_id}", response_model=TutorialAdminOut)
async def update_tutorial(
    tutorial_id: uuid.UUID, payload: TutorialUpdate, coach: CurrentCoach, db: DbSession
) -> VideoTutorial:
    tutorial = await db.get(VideoTutorial, tutorial_id)
    if tutorial is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That tutorial was not found.")

    updates = payload.model_dump(exclude_unset=True)

    if updates.get("title") and updates["title"] != tutorial.title:
        tutorial.slug = await _unique_slug(
            db, VideoTutorial, updates["title"], exclude=tutorial.id
        )

    if "video_url" in updates and updates["video_url"] is not None:
        updates["video_url"] = str(updates["video_url"])
        tutorial.provider = detect_provider(updates["video_url"])
        # A new link means the old auto-thumbnail points at the wrong video.
        if "thumbnail_url" not in updates:
            guessed = default_thumbnail(updates["video_url"])
            if guessed:
                tutorial.thumbnail_url = guessed

    if "thumbnail_url" in updates and updates["thumbnail_url"] is not None:
        updates["thumbnail_url"] = str(updates["thumbnail_url"])

    for field, value in updates.items():
        setattr(tutorial, field, value)
    await db.flush()
    await db.refresh(tutorial)
    return tutorial


@router.delete("/tutorials/{tutorial_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tutorial(
    tutorial_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> None:
    """Tutorials hold no historic references, so this is a real delete. Unpublish
    from the list screen when the intent is only to hide something."""
    tutorial = await db.get(VideoTutorial, tutorial_id)
    if tutorial is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That tutorial was not found.")
    await db.delete(tutorial)
    log.info("admin.tutorial_deleted", tutorial_id=str(tutorial_id), by=str(coach.id))


@router.post("/tutorials/reorder")
async def reorder_tutorials(payload: list[uuid.UUID], coach: CurrentCoach, db: DbSession) -> dict:
    for index, tutorial_id in enumerate(payload):
        tutorial = await db.get(VideoTutorial, tutorial_id)
        if tutorial:
            tutorial.sort_order = index
    await db.flush()
    return {"status": "saved"}


# --- Uploads ------------------------------------------------------------------


@router.post("/programs/{program_id}/image")
async def upload_program_image(
    program_id: uuid.UUID,
    coach: CurrentCoach,
    db: DbSession,
    file: UploadFile = File(...),
) -> dict:
    """Replace a tier's hero image with an uploaded file."""
    program = await db.get(Program, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan was not found.")

    previous = program.image_key
    key, size = await storage.save_program_image(program_id, file)

    program.image_key = key
    program.image_external_url = None
    await db.flush()

    if previous and previous != key:
        storage.delete_file(previous)

    log.info("admin.program_image_uploaded", program_id=str(program_id), bytes=size)

    return {"image_url": f"/api/v1/programs/{program_id}/image"}


@router.post("/tutorials/upload", status_code=status.HTTP_201_CREATED)
async def upload_tutorial_video(
    coach: CurrentCoach,
    file: UploadFile = File(...),
) -> dict:
    """Take a video file and return the key to attach to a tutorial.

    Deliberately a separate step from creating the tutorial: a 400 MB upload
    should not be re-sent because the title field failed validation. The client
    uploads first, then submits the form with the key it got back.
    """
    key, content_type, size = await storage.save_tutorial_video(file)
    log.info("admin.tutorial_video_uploaded", key=key, bytes=size)
    return {"file_key": key, "content_type": content_type, "size_bytes": size}