"""Coaching media: the video tutorial library clients watch before they train.

Recordings are posted, edited and retired from the admin dashboard. Clients get
a read-only, published-only view of the same rows.
"""

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import Equipment, TrainingLevel, TutorialCategory, VideoProvider


class VideoTutorial(UUIDMixin, TimestampMixin, Base):
    """One recording. The file itself is never uploaded here — the coach pastes a
    YouTube, Vimeo or direct MP4 link, so storage and bandwidth stay off our bill
    and the platform keeps its own analytics."""

    __tablename__ = "video_tutorials"

    slug: Mapped[str] = mapped_column(String(180), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[TutorialCategory] = mapped_column(
        Enum(TutorialCategory, name="tutorial_category"),
        default=TutorialCategory.FORM_TECHNIQUE,
        nullable=False,
        index=True,
    )
    provider: Mapped[VideoProvider] = mapped_column(
        Enum(VideoProvider, name="video_provider"), default=VideoProvider.YOUTUBE, nullable=False
    )
    video_url: Mapped[str] = mapped_column(String(500), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # Discovery
    target_muscle: Mapped[str | None] = mapped_column(String(80), index=True)
    equipment: Mapped[Equipment | None] = mapped_column(Enum(Equipment, name="equipment"))
    min_level: Mapped[TrainingLevel] = mapped_column(
        Enum(TrainingLevel, name="training_level"), default=TrainingLevel.LEVEL_1, nullable=False
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, nullable=False)

    # Optional link to the movement it demonstrates, so the workout page can
    # deep-link straight to the right clip.
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("exercises.id", ondelete="SET NULL"), index=True
    )

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    exercise: Mapped["Exercise | None"] = relationship(lazy="selectin")  # noqa: F821