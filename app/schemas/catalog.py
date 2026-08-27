"""Public catalog payloads."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.enums import (
    EQUIPMENT_LABELS,
    MUSCLE_GROUP_LABELS,
    Equipment,
    ForceType,
    Mechanics,
    MuscleGroup,
    TrainingLevel,
)


class ProgramOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    level: TrainingLevel
    tagline: str
    days_per_week: int
    session_minutes: int
    price_cents: int
    billing_period: str
    description: str
    features: list[str]
    best_for: str | None = None
    image_url: str | None = None
    is_accepting_clients: bool
    sort_order: int


class ExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    muscle_group: MuscleGroup
    target_muscle: str
    secondary_muscles: list[str]
    equipment: Equipment
    mechanics: Mechanics | None = None
    force_type: ForceType | None = None
    video_url: str | None = None
    thumbnail_url: str | None = None
    source_url: str | None = None
    instructions: str | None = None
    coaching_cue: str | None = None
    min_level: TrainingLevel
    popularity: int = 0

    @property
    def has_video(self) -> bool:
        return bool(self.video_url)


class ExerciseCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    muscle_group: MuscleGroup
    target_muscle: str = Field(min_length=2, max_length=80)
    secondary_muscles: list[str] = Field(default_factory=list, max_length=8)
    equipment: Equipment = Equipment.OTHER
    mechanics: Mechanics | None = None
    force_type: ForceType | None = None
    video_url: HttpUrl
    thumbnail_url: HttpUrl | None = None
    source_url: HttpUrl | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    coaching_cue: str | None = Field(default=None, max_length=300)
    min_level: TrainingLevel = TrainingLevel.LEVEL_1
    popularity: int = Field(default=0, ge=0, le=100)


class ExerciseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=140)
    muscle_group: MuscleGroup | None = None
    target_muscle: str | None = Field(default=None, min_length=2, max_length=80)
    secondary_muscles: list[str] | None = Field(default=None, max_length=8)
    equipment: Equipment | None = None
    mechanics: Mechanics | None = None
    force_type: ForceType | None = None
    video_url: HttpUrl | None = None
    thumbnail_url: HttpUrl | None = None
    source_url: HttpUrl | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    coaching_cue: str | None = Field(default=None, max_length=300)
    min_level: TrainingLevel | None = None
    popularity: int | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class FacetOption(BaseModel):
    """
    One heading in the coach's picker, with how much sits under it.

    The count is not decoration. A coach opening "Palmar Fascia" and finding
    four movements has different expectations from one opening "Chest" and
    finding eighteen, and showing the number up front prevents the click that
    lands nowhere.
    """

    value: str
    label: str
    count: int = 0


class ExerciseFacets(BaseModel):
    """Everything the picker needs to render its two axes in one request."""

    muscle_groups: list[FacetOption]
    equipment: list[FacetOption]
    target_muscles: list[str]
    total: int
    without_video: int = 0


def muscle_group_options(counts: dict[str, int] | None = None) -> list[FacetOption]:
    counts = counts or {}
    return [
        FacetOption(value=group.value, label=label, count=counts.get(group.value, 0))
        for group, label in MUSCLE_GROUP_LABELS.items()
    ]


def equipment_options(counts: dict[str, int] | None = None) -> list[FacetOption]:
    counts = counts or {}
    return [
        FacetOption(value=item.value, label=label, count=counts.get(item.value, 0))
        for item, label in EQUIPMENT_LABELS.items()
    ]


class CatalogSyncOut(BaseModel):
    """What a catalogue import actually changed."""

    created: int
    backfilled: int
    unchanged: int
    catalog_size: int


class LinkCheckRow(BaseModel):
    exercise_id: str
    name: str
    url: str
    status: int | None = None
    error: str | None = None


class LinkCheckOut(BaseModel):
    checked: int
    ok: int
    broken: list[LinkCheckRow]


class TestimonialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_name: str
    level_label: str
    rating: int
    quote: str
    result_metric: str | None = None
    weeks_in: int | None = None