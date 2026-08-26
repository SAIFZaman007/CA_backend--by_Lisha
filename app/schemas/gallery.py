"""Gallery payloads — the public "Hall of the Coach" and its admin CRUD."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import GALLERY_CATEGORY_LABELS, GalleryCategory


class GalleryImageOut(BaseModel):
    """What a visitor — and a crawler — sees."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    caption: str | None = None
    alt_text: str
    category: GalleryCategory
    category_label: str = ""
    tags: list[str]
    image_url: str
    width: int | None = None
    height: int | None = None
    taken_on: date | None = None
    credit: str | None = None
    is_featured: bool = False


class GalleryImageAdminOut(GalleryImageOut):
    is_published: bool
    sort_order: int
    file_size_bytes: int | None = None
    created_at: datetime
    updated_at: datetime


class GallerySection(BaseModel):
    """One category with its images, ready to render as a section."""

    category: GalleryCategory
    label: str
    images: list[GalleryImageOut]


class GalleryCategoryOption(BaseModel):
    value: str
    label: str
    count: int = 0


def category_options(counts: dict[str, int] | None = None) -> list[GalleryCategoryOption]:
    counts = counts or {}
    return [
        GalleryCategoryOption(value=item.value, label=label, count=counts.get(item.value, 0))
        for item, label in GALLERY_CATEGORY_LABELS.items()
    ]


class GalleryImageCreate(BaseModel):
    # The key returned by the upload endpoint. Bytes go up first on their own
    # request, exactly as tutorial videos do, so a validation slip on the title
    # never costs the coach a re-upload.
    image_key: str = Field(min_length=4, max_length=300)
    title: str = Field(min_length=2, max_length=160)

    # Required, and validated against being a copy of the title.
    #
    # Alt text is what Google Images indexes and what a screen reader reads
    # out. "Coach Auto" as alt text on twelve photos tells neither of them
    # anything, so the minimum length forces a real sentence.
    alt_text: str = Field(min_length=8, max_length=300)

    caption: str | None = Field(default=None, max_length=2000)
    category: GalleryCategory = GalleryCategory.COACHING
    tags: list[str] = Field(default_factory=list, max_length=10)
    taken_on: date | None = None
    credit: str | None = Field(default=None, max_length=160)
    is_published: bool = True
    is_featured: bool = False
    sort_order: int = Field(default=0, ge=0, le=9999)
    width: int | None = Field(default=None, ge=1, le=20_000)
    height: int | None = Field(default=None, ge=1, le=20_000)
    file_size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in value:
            cleaned = tag.strip().lower()[:40]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @field_validator("alt_text")
    @classmethod
    def _meaningful_alt(cls, value: str) -> str:
        cleaned = value.strip()
        lowered = cleaned.lower()
        # "Image of…" and "Photo of…" are the two phrases screen readers
        # already announce, so they waste the first three words of the only
        # description a blind visitor gets.
        for prefix in ("image of", "photo of", "picture of", "a photo of", "an image of"):
            if lowered.startswith(prefix):
                raise ValueError(
                    "Describe what is in the photo directly — screen readers already "
                    'say "image", so "Photo of…" repeats itself.'
                )
        return cleaned


class GalleryImageUpdate(BaseModel):
    image_key: str | None = Field(default=None, min_length=4, max_length=300)
    title: str | None = Field(default=None, min_length=2, max_length=160)
    alt_text: str | None = Field(default=None, min_length=8, max_length=300)
    caption: str | None = Field(default=None, max_length=2000)
    category: GalleryCategory | None = None
    tags: list[str] | None = Field(default=None, max_length=10)
    taken_on: date | None = None
    credit: str | None = Field(default=None, max_length=160)
    is_published: bool | None = None
    is_featured: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)
    width: int | None = Field(default=None, ge=1, le=20_000)
    height: int | None = Field(default=None, ge=1, le=20_000)
    file_size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        seen: list[str] = []
        for tag in value:
            cleaned = tag.strip().lower()[:40]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen


class GalleryUploadOut(BaseModel):
    """What the upload endpoint hands back for the create form to submit.

    No preview URL. The dashboard previews the local File with
    `URL.createObjectURL` before it ever leaves the browser, which is instant
    and costs the server nothing — a round trip to fetch back bytes the client
    already holds would be slower and add a route that exists only to serve
    an unpublished image.
    """

    image_key: str
    width: int
    height: int
    file_size_bytes: int


class GalleryReorder(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)