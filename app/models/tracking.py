"""Everything the client enters about themselves over time:
weight, tape measurements, check-in photos, sleep and cardio."""

import uuid
from datetime import date, time

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import CardioType, DataSource, Intensity, PhotoPose


class WeightLog(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "weight_logs"
    __table_args__ = (UniqueConstraint("client_id", "log_date"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    weight_kg: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))


class BodyMeasurement(UUIDMixin, TimestampMixin, Base):
    """Tape measurements — the coach assesses bust/chest, waist and hips at intake
    and at every check-in."""

    __tablename__ = "body_measurements"
    __table_args__ = (UniqueConstraint("client_id", "log_date"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    chest_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    waist_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    hips_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    left_arm_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    right_arm_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    left_thigh_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    right_thigh_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    neck_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    body_fat_pct: Mapped[float | None] = mapped_column(Numeric(4, 1))
    note: Mapped[str | None] = mapped_column(String(300))


class ProgressPhoto(UUIDMixin, TimestampMixin, Base):
    """Check-in photos. Private to the client and their coach — never public."""

    __tablename__ = "progress_photos"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pose: Mapped[PhotoPose] = mapped_column(
        Enum(PhotoPose, name="photo_pose"), default=PhotoPose.FRONT, nullable=False
    )
    file_key: Mapped[str] = mapped_column(String(400), nullable=False)
    content_type: Mapped[str] = mapped_column(String(60), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
    shared_with_coach: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SleepLog(UUIDMixin, TimestampMixin, Base):
    """Nightly sleep entry — one per date."""

    __tablename__ = "sleep_logs"
    __table_args__ = (UniqueConstraint("client_id", "log_date"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    bedtime: Mapped[time | None] = mapped_column(Time)
    wake_time: Mapped[time | None] = mapped_column(Time)
    hours_slept: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    quality: Mapped[int | None] = mapped_column(Integer)  # 1–5
    awakenings: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class CardioLog(UUIDMixin, TimestampMixin, Base):
    """A cardio bout. `source` records whether it was typed in by hand or read
    off a fitness watch."""

    __tablename__ = "cardio_logs"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    activity_type: Mapped[CardioType] = mapped_column(
        Enum(CardioType, name="cardio_type"), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_km: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_heart_rate: Mapped[int | None] = mapped_column(Integer)
    calories_burned: Mapped[int | None] = mapped_column(Integer)
    intensity: Mapped[Intensity] = mapped_column(
        Enum(Intensity, name="intensity"), default=Intensity.MODERATE, nullable=False
    )
    source: Mapped[DataSource] = mapped_column(
        Enum(DataSource, name="data_source"), default=DataSource.MANUAL, nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(300))
