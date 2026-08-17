"""Accounts, client profiles and refresh-token sessions."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import ActivityLevel, Goal, Sex, TrainingLevel, UnitSystem, UserRole


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # The coach trades publicly as "Coach Auto"; the legal name is never exposed.
    display_name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.CLIENT, nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped["ClientProfile"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def public_name(self) -> str:
        return self.display_name or self.full_name


class ClientProfile(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "client_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # Intake / assessment
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[Sex | None] = mapped_column(Enum(Sex, name="sex"))
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    starting_weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    current_weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    goal_weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    goal: Mapped[Goal] = mapped_column(Enum(Goal, name="goal"), default=Goal.CUT, nullable=False)
    activity_level: Mapped[ActivityLevel] = mapped_column(
        Enum(ActivityLevel, name="activity_level"), default=ActivityLevel.LIGHT, nullable=False
    )
    unit_system: Mapped[UnitSystem] = mapped_column(
        Enum(UnitSystem, name="unit_system"), default=UnitSystem.IMPERIAL, nullable=False
    )

    # Coaching assignment
    level: Mapped[TrainingLevel] = mapped_column(
        Enum(TrainingLevel, name="training_level"), default=TrainingLevel.LEVEL_1, nullable=False
    )
    phase: Mapped[str | None] = mapped_column(String(60), default="Cut Phase")
    program_start_date: Mapped[date | None] = mapped_column(Date)
    program_week: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    program_total_weeks: Mapped[int] = mapped_column(Integer, default=12, nullable=False)

    # Targets
    calorie_target: Mapped[int | None] = mapped_column(Integer)
    protein_target_g: Mapped[int | None] = mapped_column(Integer)
    carb_target_g: Mapped[int | None] = mapped_column(Integer)
    fat_target_g: Mapped[int | None] = mapped_column(Integer)
    weekly_workout_target: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    sleep_target_hours: Mapped[float] = mapped_column(Numeric(3, 1), default=8.0, nullable=False)
    weekly_cardio_target_min: Mapped[int] = mapped_column(Integer, default=150, nullable=False)

    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago", nullable=False)
    medical_notes: Mapped[str | None] = mapped_column(Text)
    coach_notes: Mapped[str | None] = mapped_column(Text)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class RefreshSession(UUIDMixin, TimestampMixin, Base):
    """One row per issued refresh token. Enables server-side revocation."""

    __tablename__ = "refresh_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
