"""The public gallery — "Hall of the Coach".

Unauthenticated on purpose. Everything here is marketing imagery the client
wants found: transformations, coaching shots, competition photos,
certifications. It is served with long cache headers and listed in the sitemap,
which is exactly the opposite of how `progress.py` treats a check-in photo, and
the two must never be confused. Nothing in this module touches client data.
"""

import uuid
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.core.config import settings
from app.core.deps import DbSession
from app.models.enums import GALLERY_CATEGORY_LABELS, GalleryCategory
from app.models.gallery import GalleryImage
from app.schemas.gallery import (
    GalleryCategoryOption,
    GalleryImageOut,
    GallerySection,
    category_options,
)
from app.services import storage

router = APIRouter(prefix="/gallery", tags=["gallery"])


def image_url(image: GalleryImage) -> str:
    """The public, cacheable address of one gallery image.

    A path rather than an absolute URL, so the same response works behind the
    nginx proxy in production and Vite's dev proxy locally. The frontend and
    the sitemap builder are the two places that need it absolute, and both
    already know the canonical origin.
    """
    return f"{settings.API_V1_PREFIX}/gallery/{image.id}/file"


def serialise(image: GalleryImage) -> GalleryImageOut:
    return GalleryImageOut(
        id=image.id,
        slug=image.slug,
        title=image.title,
        caption=image.caption,
        alt_text=image.alt_text,
        category=image.category,
        category_label=GALLERY_CATEGORY_LABELS.get(image.category, image.category.value),
        tags=image.tags,
        image_url=image_url(image),
        width=image.width,
        height=image.height,
        taken_on=image.taken_on,
        credit=image.credit,
        is_featured=image.is_featured,
    )


@router.get("", response_model=list[GalleryImageOut])
async def list_images(
    db: DbSession,
    category: GalleryCategory | None = None,
    featured_only: bool = Query(False),
    limit: int = Query(120, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[GalleryImageOut]:
    stmt = (
        select(GalleryImage)
        .where(GalleryImage.is_published.is_(True))
        .order_by(
            GalleryImage.is_featured.desc(),
            GalleryImage.sort_order,
            GalleryImage.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if category:
        stmt = stmt.where(GalleryImage.category == category)
    if featured_only:
        stmt = stmt.where(GalleryImage.is_featured.is_(True))

    rows = (await db.execute(stmt)).scalars().all()
    return [serialise(image) for image in rows]


@router.get("/sections", response_model=list[GallerySection])
async def list_sections(
    db: DbSession,
    per_category: int = Query(60, ge=1, le=200),
) -> list[GallerySection]:
    """Every published image, already grouped by category.

    One request rather than one per heading. The gallery page renders a
    section per category and a client with eight categories would otherwise
    open eight connections on first paint — which is the difference between a
    fast Largest Contentful Paint and a slow one on the page most likely to be
    someone's first impression.
    """
    rows = (
        (
            await db.execute(
                select(GalleryImage)
                .where(GalleryImage.is_published.is_(True))
                .order_by(
                    GalleryImage.category,
                    GalleryImage.is_featured.desc(),
                    GalleryImage.sort_order,
                    GalleryImage.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[GalleryCategory, list[GalleryImage]] = defaultdict(list)
    for image in rows:
        if len(grouped[image.category]) < per_category:
            grouped[image.category].append(image)

    # Iterating the label map rather than the grouped dict keeps the section
    # order stable and editorial, instead of whatever the enum comparison or
    # insertion order happens to produce.
    return [
        GallerySection(
            category=category,
            label=label,
            images=[serialise(image) for image in grouped[category]],
        )
        for category, label in GALLERY_CATEGORY_LABELS.items()
        if grouped.get(category)
    ]


@router.get("/categories", response_model=list[GalleryCategoryOption])
async def list_categories(
    db: DbSession,
) -> list[GalleryCategoryOption]:
    counts = dict(
        (
            await db.execute(
                select(GalleryImage.category, func.count(GalleryImage.id))
                .where(GalleryImage.is_published.is_(True))
                .group_by(GalleryImage.category)
            )
        ).all()
    )
    return category_options({key.value: value for key, value in counts.items()})


@router.get("/{image_id}/file")
async def gallery_file(
    image_id: uuid.UUID, db: DbSession
) -> FileResponse:
    """The bytes. Public, immutable, cached hard.

    Safe to cache for a year because replacing an image writes a new random
    filename and a new row id — see `storage.save_gallery_image`. A URL that
    has been cached at the edge can therefore never start serving a different
    photo than the one it was cached for.
    """
    image = await db.get(GalleryImage, image_id)
    if image is None or not image.is_published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That image was not found.")

    return FileResponse(
        storage.resolve_path(image.image_key),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/{slug}", response_model=GalleryImageOut)
async def get_image(slug: str, db: DbSession) -> GalleryImageOut:
    image = (
        await db.execute(
            select(GalleryImage).where(
                GalleryImage.slug == slug, GalleryImage.is_published.is_(True)
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That image was not found.")
    return serialise(image)