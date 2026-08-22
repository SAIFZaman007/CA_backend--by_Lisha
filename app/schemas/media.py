"""Video tutorial payloads — read models for clients, write models for the coach."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.models.enums import Equipment, TrainingLevel, TutorialCategory, VideoProvider

# Accepts the four shapes people actually paste: watch links, share links,
# /embed/ links and bare IDs on the query string.
_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_VIMEO_ID = re.compile(r"vimeo\.com/(?:video/|channels/[^/]+/|groups/[^/]+/videos/)?(\d{6,})")


def detect_provider(url: str) -> VideoProvider:
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return VideoProvider.YOUTUBE
    if "vimeo.com" in lowered:
        return VideoProvider.VIMEO
    return VideoProvider.DIRECT


def extract_video_id(url: str) -> str | None:
    """The bare ID, used to build an embed URL and a free thumbnail."""
    match = _YOUTUBE_ID.search(url) or _VIMEO_ID.search(url)
    return match.group(1) if match else None


def default_thumbnail(url: str) -> str | None:
    """YouTube publishes a thumbnail per video, so the coach need not find one."""
    if detect_provider(url) is VideoProvider.YOUTUBE:
        video_id = extract_video_id(url)
        if video_id:
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


class TutorialOut(BaseModel):
    """What a client sees. Note there is no `created_by` — the coach trades as
    Coach Auto and no legal name is ever exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    summary: str | None = None
    description: str | None = None
    category: TutorialCategory
    provider: VideoProvider
    video_url: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    target_muscle: str | None = None
    equipment: Equipment | None = None
    min_level: TrainingLevel
    tags: list[str]
    exercise_id: uuid.UUID | None = None
    is_featured: bool


class TutorialAdminOut(TutorialOut):
    """Adds the operational columns the dashboard needs."""

    is_published: bool
    sort_order: int
    view_count: int
    created_at: datetime
    updated_at: datetime


class TutorialCreate(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    # Exactly one source: a hosting link, or the key returned by the upload
    # endpoint. Enforced below rather than left to the database, so the coach
    # gets a sentence instead of an integrity error.
    video_url: HttpUrl | None = None
    file_key: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=6000)
    category: TutorialCategory = TutorialCategory.FORM_TECHNIQUE
    thumbnail_url: HttpUrl | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    target_muscle: str | None = Field(default=None, max_length=80)
    equipment: Equipment | None = None
    min_level: TrainingLevel = TrainingLevel.LEVEL_1
    tags: list[str] = Field(default_factory=list, max_length=12)
    exercise_id: uuid.UUID | None = None
    is_published: bool = True
    is_featured: bool = False
    sort_order: int = Field(default=0, ge=0, le=9999)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in value:
            cleaned = tag.strip().lower()[:40]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @model_validator(mode="after")
    def _one_source(self) -> "TutorialCreate":
        if not self.video_url and not self.file_key:
            raise ValueError("Add a video link, or upload a video file.")
        return self

    @model_validator(mode="after")
    def _fill_thumbnail(self) -> "TutorialCreate":
        if self.video_url is None:
            return self
        if self.thumbnail_url is None:
            guessed = default_thumbnail(str(self.video_url))
            if guessed:
                object.__setattr__(self, "thumbnail_url", HttpUrl(guessed))
        return self


class TutorialUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=160)
    video_url: HttpUrl | None = None
    file_key: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=6000)
    category: TutorialCategory | None = None
    thumbnail_url: HttpUrl | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    target_muscle: str | None = Field(default=None, max_length=80)
    equipment: Equipment | None = None
    min_level: TrainingLevel | None = None
    tags: list[str] | None = Field(default=None, max_length=12)
    exercise_id: uuid.UUID | None = None
    is_published: bool | None = None
    is_featured: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)

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


class TutorialFilters(BaseModel):
    categories: list[str]
    target_muscles: list[str]
    equipment: list[str]