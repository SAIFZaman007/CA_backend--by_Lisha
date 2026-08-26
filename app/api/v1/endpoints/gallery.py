"""Gallery management — the coach's side of the Hall of the Coach.

Add, edit, reorder, publish and delete, all from the dashboard. Upload is its
own endpoint for the same reason tutorial video upload is: the bytes go up
first and independently, so a slip in the title field never costs a re-upload.
"""

import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from slugify import slugify
from sqlalchemy import select

from app.api.v1.endpoints.gallery import serialise
from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.models.enums import GalleryCategory
from app.models.gallery import GalleryImage
from app.schemas.gallery import (
    GalleryImageAdminOut,
    GalleryImageCreate,
    GalleryImageUpdate,
    GalleryReorder,
    GalleryUploadOut,
)
from app.services import storage

router = APIRouter()
log = get_logger("admin.gallery")


async def _unique_slug(db: DbSession, title: str, *, exclude: uuid.UUID | None = None) -> str:
    """A URL-safe slug that is not already taken.

    Two photos legitimately share a title — "Week 12" happens every quarter —
    so a collision is normal operation rather than an error worth surfacing.
    Suffixing quietly is the right behaviour; failing the save is not.
    """
    base = slugify(title)[:160] or "gallery-image"
    candidate = base
    suffix = 2
    while True:
        stmt = select(GalleryImage.id).where(GalleryImage.slug == candidate)
        if exclude:
            stmt = stmt.where(GalleryImage.id != exclude)
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}-{suffix}"[:180]
        suffix += 1


def _admin_out(image: GalleryImage) -> GalleryImageAdminOut:
    base = serialise(image)
    return GalleryImageAdminOut(
        **base.model_dump(),
        is_published=image.is_published,
        sort_order=image.sort_order,
        file_size_bytes=image.file_size_bytes,
        created_at=image.created_at,
        updated_at=image.updated_at,
    )


@router.post("/gallery/upload", response_model=GalleryUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_gallery_image(
    coach: CurrentCoach,
    file: UploadFile = File(...),
) -> GalleryUploadOut:
    """Store the bytes and hand back the key the create form submits.

    Dimensions come back with it so the create form can show a real preview,
    and so `width`/`height` land on the row — the public grid needs them to
    reserve layout space before the images arrive, or every photo that loads
    shoves the page around.
    """
    key, size, width, height = await storage.save_gallery_image(file)
    log.info("gallery.uploaded", bytes=size, width=width, height=height)
    return GalleryUploadOut(
        image_key=key,
        width=width,
        height=height,
        file_size_bytes=size,
    )


@router.get("/gallery", response_model=list[GalleryImageAdminOut])
async def list_gallery(
    coach: CurrentCoach,
    db: DbSession,
    category: GalleryCategory | None = None,
    include_unpublished: bool = Query(True),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[GalleryImageAdminOut]:
    stmt = (
        select(GalleryImage)
        .order_by(
            GalleryImage.category,
            GalleryImage.sort_order,
            GalleryImage.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if category:
        stmt = stmt.where(GalleryImage.category == category)
    if not include_unpublished:
        stmt = stmt.where(GalleryImage.is_published.is_(True))

    rows = (await db.execute(stmt)).scalars().all()
    return [_admin_out(image) for image in rows]


@router.post("/gallery", response_model=GalleryImageAdminOut, status_code=status.HTTP_201_CREATED)
async def create_gallery_image(
    payload: GalleryImageCreate, coach: CurrentCoach, db: DbSession
) -> GalleryImageAdminOut:
    # Confirm the uploaded file is really there before writing a row that
    # points at it. Otherwise a stale key from an abandoned tab produces a
    # gallery entry that renders as a broken image on the public page.
    storage.resolve_path(payload.image_key)

    data = payload.model_dump()
    image = GalleryImage(
        slug=await _unique_slug(db, payload.title),
        created_by_id=coach.id,
        **data,
    )
    db.add(image)
    await db.flush()

    log.info("gallery.created", image_id=str(image.id), category=image.category.value)
    return _admin_out(image)


@router.patch("/gallery/{image_id}", response_model=GalleryImageAdminOut)
async def update_gallery_image(
    image_id: uuid.UUID, payload: GalleryImageUpdate, coach: CurrentCoach, db: DbSession
) -> GalleryImageAdminOut:
    image = await db.get(GalleryImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That image was not found.")

    updates = payload.model_dump(exclude_unset=True)

    # Replacing the file: verify the new one, then delete the old one only
    # after the row has been repointed. Deleting first would leave a gap where
    # the page has no image at all if the write below fails.
    old_key: str | None = None
    if "image_key" in updates and updates["image_key"] and updates["image_key"] != image.image_key:
        storage.resolve_path(updates["image_key"])
        old_key = image.image_key

    if "title" in updates and updates["title"]:
        image.slug = await _unique_slug(db, updates["title"], exclude=image.id)

    for field, value in updates.items():
        setattr(image, field, value)
    await db.flush()

    if old_key:
        storage.delete_file(old_key)

    log.info("gallery.updated", image_id=str(image_id))
    return _admin_out(image)


@router.post("/gallery/reorder", response_model=list[GalleryImageAdminOut])
async def reorder_gallery(
    payload: GalleryReorder, coach: CurrentCoach, db: DbSession
) -> list[GalleryImageAdminOut]:
    """Persist a drag-and-drop reorder as one transaction.

    Position is taken from the order of the submitted ids rather than from a
    per-item index the client calculates. The client already knows the order —
    it just dragged it — and recomputing indices here removes an entire class
    of off-by-one bug where two items claim position 3.
    """
    rows = (
        (await db.execute(select(GalleryImage).where(GalleryImage.id.in_(payload.ids))))
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    missing = [str(i) for i in payload.ids if i not in by_id]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One of those images no longer exists. Refresh and try again.",
        )

    for position, image_id in enumerate(payload.ids):
        by_id[image_id].sort_order = position
    await db.flush()

    return [_admin_out(by_id[image_id]) for image_id in payload.ids]


@router.delete("/gallery/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gallery_image(
    image_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> None:
    """Remove the row and the file behind it.

    A hard delete, unlike the soft delete used for exercises. Nothing
    references a gallery image — no session log, no plan, no historic record —
    so keeping the row would only keep the file on the volume, and the coach
    who deletes a photo means it to be gone.
    """
    image = await db.get(GalleryImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That image was not found.")

    key = image.image_key
    await db.delete(image)
    await db.flush()
    storage.delete_file(key)

    log.info("gallery.deleted", image_id=str(image_id))