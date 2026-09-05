"""Coach ↔ client messaging, consultation bookings and website enquiries."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import AttachmentKind, BookingStatus, LeadStatus, TrainingLevel


class MessageThread(UUIDMixin, TimestampMixin, Base):
    """
    One client's single conversation with the coach.

    "Single" is now enforced rather than assumed. Nothing used to stop a second
    row appearing for the same client — a race between the portal opening a
    thread and the inbox opening one was enough — and the result was a client
    and a coach unknowingly writing into two halves of the same conversation.
    Migration 0008 merged the duplicates and added this constraint; it is
    declared here as well so autogenerate does not read it as drift and propose
    dropping it.
    """

    __tablename__ = "message_threads"
    __table_args__ = (UniqueConstraint("client_id", name="uq_message_threads_client_id"),)

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

    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    thread: Mapped[MessageThread] = relationship(back_populates="messages")
    attachments: Mapped[list["MessageAttachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageAttachment.created_at",
        lazy="selectin",
    )


class MessageAttachment(UUIDMixin, TimestampMixin, Base):
    """
    An image a client sent their coach — a loaded bar, a meal, a scale.

    `message_id` is nullable, and that is the whole design. Bytes go up on
    their own request and land here unattached, owned by the uploader. The
    message that references them is written afterwards, in a second call, and
    binds them.

    Two things fall out of that. A slow upload over gym wi-fi does not block
    the text box, and a validation failure on the message does not make anyone
    re-send a 6 MB photo. The cost is orphan rows when someone attaches a photo
    and then closes the tab; `purge_orphan_attachments` sweeps those.
    """

    __tablename__ = "message_attachments"
    __table_args__ = (

        Index(
            "ix_message_attachments_orphans",
            "uploaded_by_id",
            "created_at",
            postgresql_where=text("message_id IS NULL"),
        ),
    )

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
    )

    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    kind: Mapped[AttachmentKind] = mapped_column(
        Enum(AttachmentKind, name="attachment_kind"),
        default=AttachmentKind.IMAGE,
        nullable=False,
    )
    file_key: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), default="image/jpeg", nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    original_name: Mapped[str | None] = mapped_column(String(200))

    message: Mapped["Message | None"] = relationship(back_populates="attachments")


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