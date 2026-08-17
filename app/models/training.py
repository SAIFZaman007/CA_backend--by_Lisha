"""Training: assigned plans, the days inside them, and what the client actually lifted."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import SessionStatus, TrainingLevel


class WorkoutPlan(UUIDMixin, TimestampMixin, Base):
    """A block of training assigned to one client. `is_custom` marks plans the
    client built themselves rather than ones the coach prescribed."""

    __tablename__ = "workout_plans"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("programs.id", ondelete="SET NULL")
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    level: Mapped[TrainingLevel] = mapped_column(
        Enum(TrainingLevel, name="training_level"), nullable=False
    )
    week_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_weeks: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    days: Mapped[list["WorkoutDay"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="WorkoutDay.order_index",
        lazy="selectin",
    )


class WorkoutDay(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "workout_days"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workout_plans.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)  # "Day A"
    focus: Mapped[str] = mapped_column(String(80), nullable=False)  # "Lower Body"
    day_of_week: Mapped[int | None] = mapped_column(Integer)  # 0 = Monday
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=55, nullable=False)

    plan: Mapped[WorkoutPlan] = relationship(back_populates="days")
    exercises: Mapped[list["WorkoutDayExercise"]] = relationship(
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="WorkoutDayExercise.order_index",
        lazy="selectin",
    )


class WorkoutDayExercise(UUIDMixin, TimestampMixin, Base):
    """A prescription: this movement, this many sets, this rep range."""

    __tablename__ = "workout_day_exercises"

    day_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workout_days.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    exercise_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("exercises.id", ondelete="RESTRICT"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sets: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    rep_range: Mapped[str] = mapped_column(String(30), default="8-12", nullable=False)
    rest_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    tempo: Mapped[str | None] = mapped_column(String(20))
    target_weight_kg: Mapped[float | None] = mapped_column(Numeric(6, 2))
    coach_note: Mapped[str | None] = mapped_column(String(300))

    day: Mapped[WorkoutDay] = relationship(back_populates="exercises")
    exercise: Mapped["Exercise"] = relationship(lazy="selectin")  # noqa: F821


class WorkoutSession(UUIDMixin, TimestampMixin, Base):
    """One training day actually performed."""

    __tablename__ = "workout_sessions"
    __table_args__ = (UniqueConstraint("client_id", "day_id", "session_date"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workout_days.id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"), default=SessionStatus.IN_PROGRESS, nullable=False
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    calories_burned: Mapped[int | None] = mapped_column(Integer)
    client_notes: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sets: Mapped[list["SetLog"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )


class SetLog(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "set_logs"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workout_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    day_exercise_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workout_day_exercises.id", ondelete="CASCADE"),
        nullable=False,
    )
    set_number: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_kg: Mapped[float | None] = mapped_column(Numeric(6, 2))
    reps: Mapped[int | None] = mapped_column(Integer)
    rpe: Mapped[float | None] = mapped_column(Numeric(3, 1))
    is_completed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    session: Mapped[WorkoutSession] = relationship(back_populates="sets")
