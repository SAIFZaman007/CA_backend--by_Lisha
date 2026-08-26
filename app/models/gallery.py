"""The Gallery — "Hall of the Coach".

Public, unauthenticated marketing imagery: transformations, coaching shots,
competition photos, certifications. Distinct from `ProgressPhoto`, which is
private client health data behind a signed URL, and from `Program.image_key`,
which is one hero shot bolted to a pricing tier.

The separation matters. Everything in this table is deliberately world-readable
and served with long cache headers so it can be crawled and indexed — which is
exactly what you must never do to a check-in photo.
"""

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import GalleryCategory


class GalleryImage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "gallery_images"
    __table_args__ = (
        # The public read is always "published, in this category, in order",
        # so index precisely that.
        Index("ix_gallery_published_order", "is_published", "category", "sort_order"),
    )

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), unique=True, index=True, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)

    # Not optional, and not a copy of the title.
    #
    # Alt text is the single highest-leverage SEO field on an image — it is what
    # Google Images indexes and what a screen reader announces. Making it
    # NOT NULL is the only reliable way to stop it being skipped: an optional
    # field on a busy admin form is an empty field.
    alt_text: Mapped[str] = mapped_column(String(300), nullable=False)

    category: Mapped[GalleryCategory] = mapped_column(
        Enum(GalleryCategory, name="gallery_category"),
        default=GalleryCategory.COACHING,
        nullable=False,
        index=True,
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, nullable=False)

    image_key: Mapped[str] = mapped_column(String(300), nullable=False)
    # Stored at upload time so the public page can reserve the right box before
    # the bytes arrive. Without these two the gallery reflows as every image
    # lands, which is a Cumulative Layout Shift penalty on the exact page the
    # client wants ranking.
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)

    taken_on: Mapped[date | None] = mapped_column(Date)
    credit: Mapped[str | None] = mapped_column(String(160))

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )