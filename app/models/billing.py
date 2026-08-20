"""Billing: what a client has paid for, and what that entitles them to.

Stripe is the source of truth for money. These tables are a local projection of
it, kept current by webhooks, so that answering "may this person open their
workout page?" is one indexed query rather than a network call to Stripe on
every request.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import PaymentStatus, SubscriptionStatus


class Subscription(UUIDMixin, TimestampMixin, Base):
    """One client's subscription to one coaching tier.

    A client may accumulate several rows over time (upgrades, cancellations,
    resubscriptions); at most one is ever in an entitling status, which is what
    `services.entitlements` reads.
    """

    __tablename__ = "subscriptions"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Keep the row if a tier is deleted — billing history must survive the
    # catalogue being tidied up.
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("programs.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"),
        default=SubscriptionStatus.INCOMPLETE,
        nullable=False,
        index=True,
    )

    stripe_customer_id: Mapped[str | None] = mapped_column(String(80), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(80))

    # Snapshot of what was charged, so an old invoice still reads correctly
    # after the coach changes the tier's list price.
    price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    billing_period: Mapped[str] = mapped_column(String(20), default="month", nullable=False)

    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
    program: Mapped["Program | None"] = relationship(lazy="selectin")  # noqa: F821


class Payment(UUIDMixin, TimestampMixin, Base):
    """One invoice or charge. Written from webhooks; never edited by hand."""

    __tablename__ = "payments"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.PENDING, nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)

    stripe_invoice_id: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(80), index=True)
    receipt_url: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(String(300))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(UUIDMixin, TimestampMixin, Base):
    """Every Stripe event id we have already processed.

    Stripe guarantees at-least-once delivery, not exactly-once — it retries on
    any non-2xx and can deliver the same event twice on a good day. Without this
    table a retried `invoice.paid` writes a duplicate payment row. The unique
    constraint on `stripe_event_id` is the actual guard; the rest is for
    debugging a delivery that went wrong.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),)

    stripe_event_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)