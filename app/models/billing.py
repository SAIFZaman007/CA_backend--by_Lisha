"""Billing: what a client has paid for, and what that entitles them to.

Stripe is the source of truth for money. These tables are a local projection of
it, kept current by webhooks, so that answering "may this person open their
workout page?" is one indexed query rather than a network call to Stripe on
every request.
"""

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
)
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

    # --- Scheduled change (downgrade) ----------------------------------------
    #
    # A downgrade is never applied on the spot. The client has paid through the
    # end of the period and keeps that tier until it ends; the tier they are
    # moving *to* waits here and the webhook applies it when Stripe bills the
    # next period. Storing the intent locally as well as in Stripe means the
    # portal can say "Level 2 from 14 March" without a round trip, and means a
    # missed webhook is recoverable rather than invisible.
    scheduled_program_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("programs.id", ondelete="SET NULL")
    )
    scheduled_price_cents: Mapped[int | None] = mapped_column(Integer)
    scheduled_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stripe_schedule_id: Mapped[str | None] = mapped_column(String(80))

    # --- Why they left --------------------------------------------------------
    #
    # Collected at cancellation, never required. A coded reason aggregates; the
    # comment is where the actual information usually is.
    cancellation_reason: Mapped[str | None] = mapped_column(String(60))
    cancellation_comment: Mapped[str | None] = mapped_column(Text)

    # --- Dunning --------------------------------------------------------------
    #
    # Stripe retries a failed charge on its own schedule. These mirror where in
    # that process the subscription is, so the portal can show "your card was
    # declined — update it before the 14th" instead of a client discovering the
    # problem when their programme disappears.
    payment_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_payment_error: Mapped[str | None] = mapped_column(String(300))

    # --- Card on file ---------------------------------------------------------
    #
    # Brand, last four and expiry only. Enough for someone to recognise which
    # card is being charged; nothing that is worth stealing or that brings this
    # database into PCI scope. The full instrument stays at Stripe.
    payment_method_brand: Mapped[str | None] = mapped_column(String(40))
    payment_method_last4: Mapped[str | None] = mapped_column(String(4))
    payment_method_exp: Mapped[str | None] = mapped_column(String(7))


    client: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
    # `foreign_keys` is required, not optional tidiness.
    #
    # This table now has TWO foreign keys to `programs`: `program_id` (what the
    # client is on) and `scheduled_program_id` (what they are moving to). The
    # moment the second one was added, SQLAlchemy could no longer infer which
    # column this relationship traverses and refused to configure the mapper at
    # all:
    #
    #     AmbiguousForeignKeysError: Could not determine join condition between
    #     parent/child tables on relationship Subscription.program
    #
    # That is a startup failure, not a query failure — the app would not boot,
    # and the traceback surfaced from whatever happened to touch the ORM first
    # (here, exercise seeding), which points nowhere near the real cause.
    #
    # Naming the column as a string rather than the attribute keeps this
    # resolvable at mapper-configuration time regardless of import order.
    program: Mapped["Program | None"] = relationship(  # noqa: F821
        lazy="selectin", foreign_keys="Subscription.program_id"
    )

    # The pending tier is deliberately NOT a relationship. It is read on one
    # screen, by one query, for the handful of subscriptions that have one — a
    # second `selectin` would add a round trip to every subscription load in
    # the application to serve that. `db.get(Program, ...)` at the call site is
    # the cheaper trade.


class Payment(UUIDMixin, TimestampMixin, Base):
    """One invoice or charge. Written from webhooks; never edited by hand."""

    __tablename__ = "payments"
    __table_args__ = (
        # Always read as "this client's history, newest first".
        Index("ix_payments_client_created", "client_id", "created_at"),
    )

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

    # Stripe's own invoice artefacts, cached at webhook time. Billing history
    # then renders from one local query rather than N calls to Stripe, and
    # still reads correctly when Stripe is briefly unreachable.
    invoice_pdf_url: Mapped[str | None] = mapped_column(String(500))
    invoice_number: Mapped[str | None] = mapped_column(String(60))
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Why a charge failed, in Stripe's words, and how many attempts it has had.
    # Both are what turns "payment failed" into something a person can act on.
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    attempt_count: Mapped[int | None] = mapped_column(Integer)



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