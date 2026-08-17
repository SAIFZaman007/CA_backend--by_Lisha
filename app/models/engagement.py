"""Coach ↔ client messaging, consultation bookings and website enquiries."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import BookingStatus, LeadStatus, TrainingLevel


class MessageThread(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "message_threads"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    coach_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subject: Mapped[str] = mapped_column(String(160), default="Coaching check-in", nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
    )


class Message(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("message_threads.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    thread: Mapped[MessageThread] = relationship(back_populates="messages")


class ConsultationBooking(UUIDMixin, TimestampMixin, Base):
    """A request for a live chat slot with the coach."""

    __tablename__ = "consultation_bookings"

    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago", nullable=False)
    topic: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"), default=BookingStatus.REQUESTED, nullable=False
    )
    coach_notes: Mapped[str | None] = mapped_column(Text)


class Lead(UUIDMixin, TimestampMixin, Base):
    """Someone who filled in the "Start your transformation" form."""

    __tablename__ = "leads"

    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    level_interest: Mapped[TrainingLevel | None] = mapped_column(
        Enum(TrainingLevel, name="training_level")
    )
    primary_goal: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60), default="website", nullable=False)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status"), default=LeadStatus.NEW, nullable=False, index=True
    )
    consent_marketing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
