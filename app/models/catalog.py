"""Public catalog: coaching programs, the exercise library and client results."""

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import Equipment, TrainingLevel


class Program(UUIDMixin, TimestampMixin, Base):
    """A purchasable coaching tier. Prices are per-month in minor units."""

    __tablename__ = "programs"

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[TrainingLevel] = mapped_column(
        Enum(TrainingLevel, name="training_level"), nullable=False
    )
    tagline: Mapped[str] = mapped_column(String(200), nullable=False)
    days_per_week: Mapped[int] = mapped_column(Integer, nullable=False)
    session_minutes: Mapped[int] = mapped_column(Integer, default=55, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    billing_period: Mapped[str] = mapped_column(String(20), default="month", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    features: Mapped[list[str]] = mapped_column(ARRAY(String(200)), default=list, nullable=False)
    best_for: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_accepting_clients: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Exercise(UUIDMixin, TimestampMixin, Base):
    """Exercise library. Each movement carries a demonstration video link."""

    __tablename__ = "exercises"

    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(140), nullable=False, index=True)
    target_muscle: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    secondary_muscles: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), default=list, nullable=False
    )
    equipment: Mapped[Equipment] = mapped_column(
        Enum(Equipment, name="equipment"), default=Equipment.OTHER, nullable=False, index=True
    )
    video_url: Mapped[str | None] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    instructions: Mapped[str | None] = mapped_column(Text)
    coaching_cue: Mapped[str | None] = mapped_column(String(300))
    min_level: Mapped[TrainingLevel] = mapped_column(
        Enum(TrainingLevel, name="training_level"), default=TrainingLevel.LEVEL_1, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class Testimonial(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "testimonials"

    client_name: Mapped[str] = mapped_column(String(80), nullable=False)
    level_label: Mapped[str] = mapped_column(String(60), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    result_metric: Mapped[str | None] = mapped_column(String(80))
    weeks_in: Mapped[int | None] = mapped_column(Integer)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
