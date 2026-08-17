"""Public catalog payloads."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.enums import Equipment, TrainingLevel


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
    is_accepting_clients: bool
    sort_order: int


class ExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    target_muscle: str
    secondary_muscles: list[str]
    equipment: Equipment
    video_url: str | None = None
    thumbnail_url: str | None = None
    instructions: str | None = None
    coaching_cue: str | None = None
    min_level: TrainingLevel


class ExerciseCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    target_muscle: str = Field(min_length=2, max_length=80)
    secondary_muscles: list[str] = Field(default_factory=list)
    equipment: Equipment = Equipment.OTHER
    video_url: HttpUrl | None = None
    thumbnail_url: HttpUrl | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    coaching_cue: str | None = Field(default=None, max_length=300)
    min_level: TrainingLevel = TrainingLevel.LEVEL_1


class ExerciseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=140)
    target_muscle: str | None = Field(default=None, min_length=2, max_length=80)
    secondary_muscles: list[str] | None = None
    equipment: Equipment | None = None
    video_url: HttpUrl | None = None
    thumbnail_url: HttpUrl | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    coaching_cue: str | None = Field(default=None, max_length=300)
    min_level: TrainingLevel | None = None
    is_active: bool | None = None


class TestimonialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_name: str
    level_label: str
    rating: int
    quote: str
    result_metric: str | None = None
    weeks_in: int | None = None
