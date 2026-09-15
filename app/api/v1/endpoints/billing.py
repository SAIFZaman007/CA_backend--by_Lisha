"""
Subscription lifecycle: buying, changing, cancelling, paying, recovering.

The old flow stopped at the sale:

    pick a tier → Stripe Checkout → webhook → entitled → (nothing else, ever)

That is a checkout, not a subscription product. Everything a paying customer
expects to be able to do afterwards — move up a tier, move down, pause the
relationship, replace a dead card, find last month's receipt — either did not
exist or was a 409 with no route out of it. A client on Level 1 who pressed
"Subscribe" on Level 3 was told "You already have an active plan" and given
nowhere to go, which is the exact moment an upgrade turns into a cancellation.

The principles this module encodes
----------------------------------
**Upgrades are immediate, downgrades are deferred.** Someone paying more to get
more should get it now; someone paying less should keep what they already paid
for until the period they paid for runs out. Removing features the instant a
downgrade is requested is both unfair and the fastest route to a chargeback.

**Cancellation ends at the period boundary, never on the spot.** Money already
taken buys coaching already promised.

**Stripe owns the billing clock.** Deferred changes are Stripe subscription
schedules, not rows this application has to remember to act on. Anything that
depends on our process being awake at the right minute is a change that
eventually does not happen.

**The webhook is the only thing that grants or removes access.** Endpoints here
ask Stripe to do something; what actually happened comes back as an event.
A client who closes the tab is still subscribed. A client who forges a request
to the success URL is not.

**A failed payment is a conversation, not a lockout.** `past_due` stays
entitled (see `ENTITLING_STATUSES`) while Stripe retries and the client is told,
by email and in the portal, exactly what to fix and by when.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.logging import get_logger
from app.models.billing import Payment, Subscription, WebhookEvent
from app.models.catalog import Program
from app.models.enums import PaymentStatus, SubscriptionStatus, UserRole
from app.services import entitlements, stripe_gateway
from app.services.email import send_payment_failed, send_subscription_cancelled

router = APIRouter(prefix="/billing", tags=["billing"])
log = get_logger("billing")


def _dt(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --- Request bodies -----------------------------------------------------------
#
# Typed, not `dict`. The old checkout endpoint took a bare dict and hand-parsed
# a UUID out of it, which meant a malformed request produced a generic 422 with
# no field name. These give the portal something it can attach to an input.


class CheckoutRequest(BaseModel):
    program_id: uuid.UUID


class PlanChangeRequest(BaseModel):
    program_id: uuid.UUID


class CancelRequest(BaseModel):
    # Free choice, deliberately not an enum: the set of reasons a coaching
    # business wants to track changes far more often than a database migration
    # is worth. Validated for length only.
    reason: str | None = Field(default=None, max_length=60)
    comment: str | None = Field(default=None, max_length=1000)


# --- Shared helpers -----------------------------------------------------------


LEVEL_RANK = {"level_1": 1, "level_2": 2, "level_3": 3}


def _rank(program: Program | None) -> int:
    """Where a tier sits on the ladder. Higher is more.

    Ranked by `level` rather than by price, because the ladder is a product
    decision and the price is a marketing one. A promotional month where
    Level 3 costs less than Level 2 must not silently turn every upgrade into a
    downgrade with different proration behaviour.
    """
    if program is None:
        return 0
    return LEVEL_RANK.get(program.level.value, 0)


async def _require_active(db: DbSession, user_id: uuid.UUID) -> Subscription:
    subscription = await entitlements.active_subscription(db, user_id)
    if subscription is None or not subscription.stripe_subscription_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="You do not have an active plan to change. Choose one to get started.",
        )
    return subscription


def _payment_row(payment: Payment) -> dict:
    return {
        "id": str(payment.id),
        "status": payment.status.value,
        "amount_cents": payment.amount_cents,
        "currency": payment.currency,
        "description": payment.description,
        "invoice_number": payment.invoice_number,
        "receipt_url": payment.receipt_url,
        "invoice_pdf_url": payment.invoice_pdf_url,
        "failure_reason": payment.failure_reason,
        "attempt_count": payment.attempt_count,
        "period_start": _iso(payment.period_start),
        "period_end": _iso(payment.period_end),
        "paid_at": _iso(payment.paid_at),
        "created_at": _iso(payment.created_at),
    }


async def _subscription_summary(db: DbSession, subscription: Subscription | None) -> dict | None:
    """Everything the billing screen renders about one subscription.

    The card details come from Stripe rather than the local cache when a
    customer id is available, because a card replaced through Stripe's portal
    changes there first and our webhook second — and a billing page that shows
    the old card immediately after someone replaced it looks broken.
    """
    if subscription is None:
        return None

    card = None
    if subscription.stripe_customer_id and stripe_gateway.is_configured():
        card = await stripe_gateway.get_payment_method(subscription.stripe_customer_id)

    if card:
        subscription.payment_method_brand = card.get("brand")
        subscription.payment_method_last4 = card.get("last4")
        subscription.payment_method_exp = card.get("exp")
    else:
        card = {
            "brand": subscription.payment_method_brand,
            "last4": subscription.payment_method_last4,
            "exp": subscription.payment_method_exp,
        }

    scheduled = None
    if subscription.scheduled_program_id:
        pending = await db.get(Program, subscription.scheduled_program_id)
        if pending:
            scheduled = {
                "program_id": str(pending.id),
                "name": pending.name,
                "slug": pending.slug,
                "level": pending.level.value,
                "price_cents": subscription.scheduled_price_cents or pending.price_cents,
                "effective_at": _iso(subscription.scheduled_change_at),
            }

    return {
        "id": str(subscription.id),
        "status": subscription.status.value,
        "program": (
            {
                "id": str(subscription.program.id),
                "name": subscription.program.name,
                "slug": subscription.program.slug,
                "level": subscription.program.level.value,
            }
            if subscription.program
            else None
        ),
        "price_cents": subscription.price_cents,
        "currency": subscription.currency,
        "billing_period": subscription.billing_period,
        "current_period_start": _iso(subscription.current_period_start),
        "current_period_end": _iso(subscription.current_period_end),
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "canceled_at": _iso(subscription.canceled_at),
        "started_at": _iso(subscription.started_at),
        "scheduled_change": scheduled,
        "payment_method": card if card and card.get("last4") else None,
        # Dunning state, surfaced so the portal can show a banner with a
        # deadline rather than letting the client find out when access stops.
        "payment_failed_at": _iso(subscription.payment_failed_at),
        "payment_retry_at": _iso(subscription.payment_retry_at),
        "last_payment_error": subscription.last_payment_error,
        "is_past_due": subscription.status is SubscriptionStatus.PAST_DUE,
    }


# --- What the portal reads ----------------------------------------------------


@router.get("/entitlement")
async def my_entitlement(user: CurrentUser, db: DbSession) -> dict:
    """Everything the portal needs to decide what to show and what to lock.

    Called once on portal load. The feature list is a flat array of strings so
    the client can gate a panel with a simple membership test rather than
    re-deriving the tier ladder in JavaScript.
    """
    entitlement = await entitlements.entitlement_for(db, user)
    return entitlement.to_dict()


@router.get("/summary")
async def billing_summary(user: CurrentUser, db: DbSession) -> dict:
    """One request that renders the whole billing page.

    Subscription, available plans with each one already classified as the
    current plan / an upgrade / a downgrade, the card on file, and the last few
    invoices. Assembling it here rather than making the client stitch four
    calls together keeps the tier-ladder logic on the server, where it is
    already defined once and cannot drift.
    """
    subscription = await entitlements.active_subscription(db, user.id)
    current_rank = _rank(subscription.program if subscription else None)

    programs = (
        (
            await db.execute(
                select(Program).where(Program.is_active.is_(True)).order_by(Program.sort_order)
            )
        )
        .scalars()
        .all()
    )

    plans = []
    for program in programs:
        rank = _rank(program)
        is_current = bool(
            subscription and subscription.program and subscription.program.id == program.id
        )
        plans.append(
            {
                "id": str(program.id),
                "slug": program.slug,
                "name": program.name,
                "level": program.level.value,
                "tagline": program.tagline,
                "price_cents": program.price_cents,
                "billing_period": program.billing_period,
                "days_per_week": program.days_per_week,
                "features": program.features,
                "is_current": is_current,
                "is_available": program.is_accepting_clients,
                # Drives the verb on the button. Getting this from the server
                # means "Upgrade" and "Downgrade" always agree with what the
                # change endpoint will actually do.
                "change_type": (
                    "current"
                    if is_current
                    else "upgrade"
                    if rank > current_rank
                    else "downgrade"
                    if current_rank and rank < current_rank
                    else "subscribe"
                ),
            }
        )

    recent = (
        (
            await db.execute(
                select(Payment)
                .where(Payment.client_id == user.id)
                .order_by(Payment.created_at.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )

    return {
        "subscription": await _subscription_summary(db, subscription),
        "plans": plans,
        "recent_payments": [_payment_row(p) for p in recent],
        "payments_configured": stripe_gateway.is_configured(),
    }


@router.get("/history")
async def payment_history(
    user: CurrentUser, db: DbSession, limit: int = Query(50, ge=1, le=200)
) -> list[dict]:
    """Full billing history from the local projection.

    Local rather than a call to Stripe on every page load: it is one indexed
    query, it renders instantly, and it still works when Stripe is having a bad
    morning. `GET /invoices` is the reconciliation path for the rare case where
    the two might disagree.
    """
    rows = (
        (
            await db.execute(
                select(Payment)
                .where(Payment.client_id == user.id)
                .order_by(Payment.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_payment_row(p) for p in rows]


@router.get("/invoices")
async def invoices(user: CurrentUser, db: DbSession) -> list[dict]:
    """Invoices read live from Stripe.

    Used to reconcile when the local history looks incomplete — typically after
    a webhook was missed while the service was down. Returns an empty list
    rather than erroring when there is no Stripe customer yet, because "you
    have never been billed" is a valid answer, not a failure.
    """
    subscription = (
        (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.client_id == user.id,
                    Subscription.stripe_customer_id.is_not(None),
                )
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if subscription is None or not stripe_gateway.is_configured():
        return []

    return await stripe_gateway.list_invoices(customer_id=subscription.stripe_customer_id)


@router.get("/checkout/{session_id}")
async def checkout_status(session_id: str) -> dict:
    """Whether a specific Stripe Checkout Session actually completed payment.

    Called by the success page right after the Stripe redirect, to confirm
    with Stripe instead of trusting the URL alone. As the module docstring
    says: the webhook is what grants access — this endpoint only decides
    what message to show the browser while it waits.
    """
    session = await stripe_gateway.retrieve_checkout_session(session_id)
    return {"paid": session.payment_status == "paid"}


# --- Buying --------------------------------------------------------------------


@router.post("/checkout")
async def start_checkout(payload: CheckoutRequest, user: CurrentUser, db: DbSession) -> dict:
    """Open a Stripe Checkout session for a tier — first purchase only."""
    if user.role is not UserRole.CLIENT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Coach accounts already have full access and cannot subscribe.",
        )

    program = await db.get(Program, payload.program_id)
    if program is None or not program.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan is no longer available.")
    if not program.is_accepting_clients:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="That plan is full right now. Get in touch and Coach Auto will let you know "
            "when a place opens.",
        )

    existing = await entitlements.active_subscription(db, user.id)
    if existing is not None:
        # Still a 409 — this is genuinely not a checkout — but the response now
        # tells the client what to do instead of ending the conversation. The
        # portal reads `action` and redirects to the change-plan flow, which is
        # the difference between an upgrade and a cancellation.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "detail": "You already have an active plan. Change it instead of buying a "
                "second one — you will only be charged the difference.",
                "action": "change_plan",
                "current_program": existing.program.name if existing.program else None,
            },
        )

    customer_id = await stripe_gateway.ensure_customer(
        email=user.email, name=user.full_name, user_id=str(user.id)
    )
    price_id = await stripe_gateway.ensure_price(
        program_id=str(program.id),
        name=program.name,
        price_cents=program.price_cents,
        currency=settings.STRIPE_CURRENCY,
        interval=program.billing_period,
    )

    mode = "payment" if program.billing_period == "once" else "subscription"
    session = await stripe_gateway.create_checkout_session(
        customer_id=customer_id,
        price_id=price_id,
        user_id=str(user.id),
        program_id=str(program.id),
        mode=mode,
        success_url=f"{settings.FRONTEND_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.FRONTEND_URL}/checkout/cancelled?program={program.slug}",
    )

    log.info("billing.checkout_started", user_id=str(user.id), program_id=str(program.id))
    return session


# --- Changing plan --------------------------------------------------------------


@router.post("/change-plan/preview")
async def preview_change(
    payload: PlanChangeRequest, user: CurrentUser, db: DbSession
) -> dict:
    """What would happen if the client switched to this tier, before they commit.

    Nobody should press a button that charges their card without being told the
    number first. For an upgrade this returns the prorated amount due today; for
    a downgrade it returns the date the change takes effect and confirms nothing
    is charged now.
    """
    subscription = await _require_active(db, user.id)
    program = await db.get(Program, payload.program_id)
    if program is None or not program.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan is no longer available.")

    if subscription.program and subscription.program.id == program.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="That is already your current plan."
        )

    is_upgrade = _rank(program) > _rank(subscription.program)

    if not is_upgrade:
        return {
            "change_type": "downgrade",
            "program": {"name": program.name, "price_cents": program.price_cents},
            "amount_due_cents": 0,
            "effective_at": _iso(subscription.current_period_end),
            "message": (
                f"You will stay on {subscription.program.name if subscription.program else 'your current plan'} "
                "until the end of the period you have already paid for, then move to "
                f"{program.name}. Nothing is charged today."
            ),
        }

    price_id = await stripe_gateway.ensure_price(
        program_id=str(program.id),
        name=program.name,
        price_cents=program.price_cents,
        currency=settings.STRIPE_CURRENCY,
        interval=program.billing_period,
    )
    preview = await stripe_gateway.preview_plan_change(
        subscription_id=subscription.stripe_subscription_id, new_price_id=price_id
    )

    return {
        "change_type": "upgrade",
        "program": {"name": program.name, "price_cents": program.price_cents},
        "amount_due_cents": preview["amount_due_cents"],
        "currency": preview["currency"],
        "effective_at": None,  # immediate
        "message": (
            f"{program.name} starts as soon as this goes through. You are charged only the "
            "difference for the rest of this billing period."
        ),
    }


@router.post("/change-plan")
async def change_plan(payload: PlanChangeRequest, user: CurrentUser, db: DbSession) -> dict:
    """Move between tiers.

    Up is immediate and prorated. Down is queued for the period boundary. The
    direction is decided here, from the tier ladder, and never sent by the
    client — a client that could name its own `change_type` could ask for an
    immediate downgrade with a proration credit, which is a refund by another
    name.
    """
    subscription = await _require_active(db, user.id)
    program = await db.get(Program, payload.program_id)

    if program is None or not program.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That plan is no longer available.")
    if not program.is_accepting_clients:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="That plan is not taking new clients right now."
        )
    if subscription.program and subscription.program.id == program.id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="That is already your current plan.")
    if subscription.billing_period == "once":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="One-off purchases cannot be changed. Buy the plan you want instead.",
        )

    price_id = await stripe_gateway.ensure_price(
        program_id=str(program.id),
        name=program.name,
        price_cents=program.price_cents,
        currency=settings.STRIPE_CURRENCY,
        interval=program.billing_period,
    )
    is_upgrade = _rank(program) > _rank(subscription.program)

    # A pending downgrade is replaced, not stacked. Two live schedules on one
    # subscription is a state Stripe will accept and nobody can reason about.
    if subscription.stripe_schedule_id:
        await stripe_gateway.release_schedule(subscription.stripe_schedule_id)
        subscription.stripe_schedule_id = None
        subscription.scheduled_program_id = None
        subscription.scheduled_price_cents = None
        subscription.scheduled_change_at = None

    if is_upgrade:
        await stripe_gateway.change_subscription_price(
            subscription_id=subscription.stripe_subscription_id,
            new_price_id=price_id,
            program_id=str(program.id),
            prorate=True,
        )
        # The local row is updated optimistically so the portal reflects the
        # change on the next render, and corrected authoritatively when
        # `customer.subscription.updated` arrives moments later.
        subscription.program_id = program.id
        subscription.stripe_price_id = price_id
        subscription.price_cents = program.price_cents
        await db.flush()
        await entitlements.sync_profile_level(db, user.id)

        log.info("billing.upgraded", user_id=str(user.id), program_id=str(program.id))
        return {
            "status": "applied",
            "change_type": "upgrade",
            "message": f"You are on {program.name} from now. Only the difference for the rest "
            "of this period has been charged.",
        }

    result = await stripe_gateway.schedule_price_change(
        subscription_id=subscription.stripe_subscription_id,
        new_price_id=price_id,
        program_id=str(program.id),
    )
    subscription.stripe_schedule_id = result["schedule_id"]
    subscription.scheduled_program_id = program.id
    subscription.scheduled_price_cents = program.price_cents
    subscription.scheduled_change_at = _dt(result["effective_at"])
    await db.flush()

    log.info("billing.downgrade_scheduled", user_id=str(user.id), program_id=str(program.id))
    return {
        "status": "scheduled",
        "change_type": "downgrade",
        "effective_at": _iso(subscription.scheduled_change_at),
        "message": f"You keep your current plan until "
        f"{subscription.scheduled_change_at.date() if subscription.scheduled_change_at else 'the end of this period'}"
        f", then move to {program.name}.",
    }


@router.post("/change-plan/cancel")
async def cancel_scheduled_change(user: CurrentUser, db: DbSession) -> dict:
    """Call off a queued downgrade. Nothing about the current plan changes."""
    subscription = await _require_active(db, user.id)
    if not subscription.stripe_schedule_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="You have no pending plan change.")

    await stripe_gateway.release_schedule(subscription.stripe_schedule_id)
    subscription.stripe_schedule_id = None
    subscription.scheduled_program_id = None
    subscription.scheduled_price_cents = None
    subscription.scheduled_change_at = None
    await db.flush()

    log.info("billing.scheduled_change_cancelled", user_id=str(user.id))
    return {"status": "cancelled", "message": "Your plan stays as it is."}


# --- Cancelling and resuming ----------------------------------------------------


@router.post("/cancel")
async def cancel(payload: CancelRequest, user: CurrentUser, db: DbSession) -> dict:
    """Cancel at the end of the paid period, never immediately.

    Someone who has paid to the end of the month keeps their programme to the
    end of the month. The webhook flips the status when Stripe actually ends it,
    so entitlement follows money rather than intent.

    The reason is optional and stored both here and on the Stripe subscription.
    It is the cheapest research a subscription business ever gets: nine people
    writing "the sessions were too long" is a product change, and a status
    column can never tell you that.
    """
    subscription = await _require_active(db, user.id)

    await stripe_gateway.cancel_subscription(
        subscription_id=subscription.stripe_subscription_id,
        at_period_end=True,
        reason=payload.comment or payload.reason,
    )
    subscription.cancel_at_period_end = True
    subscription.cancellation_reason = payload.reason
    subscription.cancellation_comment = payload.comment
    await db.flush()

    log.info("billing.cancel_requested", user_id=str(user.id), reason=payload.reason)
    return {
        "status": "scheduled",
        "ends_at": _iso(subscription.current_period_end),
        "message": "Your plan stays active until the end of the period you have paid for. "
        "You can undo this any time before then.",
    }


@router.post("/resume")
async def resume(user: CurrentUser, db: DbSession) -> dict:
    """Undo a pending cancellation, while there is still something to undo."""
    subscription = await _require_active(db, user.id)
    if not subscription.cancel_at_period_end:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Your plan is not scheduled to end."
        )

    await stripe_gateway.resume_subscription(subscription.stripe_subscription_id)
    subscription.cancel_at_period_end = False
    subscription.cancellation_reason = None
    subscription.cancellation_comment = None
    await db.flush()

    log.info("billing.resumed", user_id=str(user.id))
    return {"status": "active", "message": "Your plan will keep renewing. Nothing else changes."}


# --- Payment method -------------------------------------------------------------


@router.post("/payment-method")
async def update_payment_method(user: CurrentUser, db: DbSession) -> dict:
    """A one-use Stripe link that opens straight on the card form.

    Deep-linked rather than dropping the client on the portal's menu. The
    single most common reason anyone follows this link is that their card was
    declined, and making them hunt for the right screen at that moment loses
    subscriptions.
    """
    subscription = (
        (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.client_id == user.id,
                    Subscription.stripe_customer_id.is_not(None),
                )
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if subscription is None or not subscription.stripe_customer_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="There is no billing account to manage yet."
        )

    url = await stripe_gateway.create_billing_portal_session(
        customer_id=subscription.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/portal/billing",
        flow="payment_method_update",
    )
    return {"url": url}


@router.post("/portal")
async def billing_portal(user: CurrentUser, db: DbSession) -> dict:
    """Stripe's full portal — cards, invoices, tax ids, everything."""
    subscription = (
        (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.client_id == user.id,
                    Subscription.stripe_customer_id.is_not(None),
                )
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if subscription is None or not subscription.stripe_customer_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="There is no billing account to manage yet."
        )

    url = await stripe_gateway.create_billing_portal_session(
        customer_id=subscription.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/portal/billing",
    )
    return {"url": url}


# --- Webhook -------------------------------------------------------------------


async def _upsert_subscription(db: DbSession, obj: dict) -> Subscription | None:
    """Write a Stripe subscription object into our projection of it."""
    metadata = obj.get("metadata") or {}
    stripe_sub_id = obj.get("id")

    subscription = (
        (
            await db.execute(
                select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
            )
        )
        .scalars()
        .first()
    )

    if subscription is None:
        try:
            client_id = uuid.UUID(metadata["user_id"])
            program_id = uuid.UUID(metadata["program_id"])
        except (KeyError, TypeError, ValueError):
            log.warning("billing.webhook_unmapped", stripe_subscription_id=stripe_sub_id)
            return None

        subscription = Subscription(
            client_id=client_id,
            program_id=program_id,
            stripe_subscription_id=stripe_sub_id,
        )
        db.add(subscription)

    items = (obj.get("items") or {}).get("data") or []
    price = items[0].get("price") if items else None

    subscription.stripe_customer_id = obj.get("customer") or subscription.stripe_customer_id
    subscription.status = SubscriptionStatus(obj.get("status", "incomplete"))
    subscription.current_period_start = _dt(obj.get("current_period_start"))
    subscription.current_period_end = _dt(obj.get("current_period_end"))
    subscription.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    subscription.canceled_at = _dt(obj.get("canceled_at"))
    subscription.started_at = _dt(obj.get("start_date")) or subscription.started_at

    if price:
        subscription.stripe_price_id = price.get("id")
        subscription.price_cents = price.get("unit_amount") or subscription.price_cents
        subscription.currency = price.get("currency") or subscription.currency
        recurring = price.get("recurring") or {}
        subscription.billing_period = recurring.get("interval") or subscription.billing_period

    # The tier follows the metadata, which `change_subscription_price` and the
    # scheduled phase both keep current. Without this, an upgrade would change
    # what the client is billed and not what they are entitled to.
    incoming_program = metadata.get("program_id")
    if incoming_program:
        try:
            program_id = uuid.UUID(incoming_program)
        except (TypeError, ValueError):
            program_id = None
        if program_id and program_id != subscription.program_id:
            subscription.program_id = program_id
            # A scheduled downgrade that has now taken effect is no longer
            # scheduled. Clearing it here is what stops the portal showing
            # "moving to Level 1 on 14 March" forever after 14 March.
            if subscription.scheduled_program_id == program_id:
                subscription.scheduled_program_id = None
                subscription.scheduled_price_cents = None
                subscription.scheduled_change_at = None
                subscription.stripe_schedule_id = None

    # A subscription that is active again has, by definition, been paid.
    if subscription.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        subscription.payment_failed_at = None
        subscription.payment_retry_at = None
        subscription.last_payment_error = None

    await db.flush()
    return subscription


async def _record_invoice(db: DbSession, obj: dict, *, paid: bool) -> Subscription | None:
    """Write one invoice into the local billing history.

    Matched to a subscription by Stripe's subscription id first and the
    customer id only as a fallback. Matching on customer alone — which is what
    this used to do — attaches the invoice to whichever subscription row was
    created most recently, and gets it wrong for any client who has ever
    upgraded, cancelled and resubscribed.
    """
    stripe_sub_id = obj.get("subscription")
    subscription = None

    if stripe_sub_id:
        subscription = (
            (
                await db.execute(
                    select(Subscription).where(
                        Subscription.stripe_subscription_id == stripe_sub_id
                    )
                )
            )
            .scalars()
            .first()
        )
    if subscription is None:
        subscription = (
            (
                await db.execute(
                    select(Subscription)
                    .where(Subscription.stripe_customer_id == obj.get("customer"))
                    .order_by(Subscription.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    if subscription is None:
        log.warning("billing.invoice_unmapped", invoice_id=obj.get("id"))
        return None

    lines = (obj.get("lines") or {}).get("data") or []
    period = (lines[0].get("period") if lines else None) or {}
    error = obj.get("last_finalization_error") or {}

    db.add(
        Payment(
            client_id=subscription.client_id,
            subscription_id=subscription.id,
            status=PaymentStatus.SUCCEEDED if paid else PaymentStatus.FAILED,
            amount_cents=obj.get("amount_paid") or obj.get("amount_due") or 0,
            currency=obj.get("currency") or settings.STRIPE_CURRENCY,
            stripe_invoice_id=obj.get("id"),
            stripe_payment_intent_id=obj.get("payment_intent"),
            receipt_url=obj.get("hosted_invoice_url"),
            invoice_pdf_url=obj.get("invoice_pdf"),
            invoice_number=obj.get("number"),
            description=obj.get("billing_reason"),
            period_start=_dt(period.get("start")),
            period_end=_dt(period.get("end")),
            failure_reason=None if paid else (error.get("message") or "The card was declined."),
            attempt_count=obj.get("attempt_count"),
            paid_at=datetime.now(UTC) if paid else None,
        )
    )

    if paid:
        subscription.payment_failed_at = None
        subscription.payment_retry_at = None
        subscription.last_payment_error = None
    else:
        subscription.payment_failed_at = datetime.now(UTC)
        subscription.payment_retry_at = _dt(obj.get("next_payment_attempt"))
        subscription.last_payment_error = error.get("message") or "The card was declined."

    await db.flush()
    return subscription


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook(
    request: Request,
    response: Response,
    db: DbSession,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
) -> dict:
    """Stripe's callback. The only thing that actually grants or removes access.

    Deliberately returns 200 for anything already processed or not recognised:
    a non-2xx makes Stripe retry, and retrying an event we have chosen to ignore
    achieves nothing but noise in the delivery log.

    The event set below is the production minimum for a subscription product.
    The lifecycle ones keep entitlement correct; the invoice ones keep billing
    history and dunning correct; `customer.subscription.trial_will_end` and the
    payment-action events are what stop a renewal failing silently.
    """
    if not stripe_signature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing signature.")

    event = stripe_gateway.verify_webhook(await request.body(), stripe_signature)
    event_id = event["id"]
    event_type = event["type"]

    # At-least-once delivery: the unique index is what actually stops a
    # duplicate `invoice.paid` from writing a second payment row.
    already = (
        (await db.execute(select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)))
        .scalars()
        .first()
    )
    if already is not None:
        return {"status": "duplicate"}

    record = WebhookEvent(stripe_event_id=event_id, event_type=event_type)
    db.add(record)
    await db.flush()

    obj = event["data"]["object"]
    obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
    client_id: uuid.UUID | None = None
    subscription: Subscription | None = None

    if event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
    }:
        subscription = await _upsert_subscription(db, obj)
        if subscription:
            client_id = subscription.client_id

        if subscription and event_type == "customer.subscription.deleted":
            await send_subscription_cancelled(
                to=subscription.client.email,
                name=subscription.client.full_name.split()[0],
                program_name=subscription.program.name if subscription.program else "your plan",
            )

    elif event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        try:
            client_id = uuid.UUID(metadata["user_id"])
            program_id = uuid.UUID(metadata["program_id"])
        except (KeyError, TypeError, ValueError):
            client_id = None
            program_id = None

        # A one-off purchase has no subscription object, so the row is created
        # here instead of by a subscription event.
        if client_id and program_id and obj.get("mode") == "payment":
            db.add(
                Subscription(
                    client_id=client_id,
                    program_id=program_id,
                    status=SubscriptionStatus.ACTIVE,
                    stripe_customer_id=obj.get("customer"),
                    price_cents=obj.get("amount_total") or 0,
                    currency=obj.get("currency") or settings.STRIPE_CURRENCY,
                    billing_period="once",
                    started_at=datetime.now(UTC),
                )
            )
            await db.flush()

    elif event_type in {"invoice.paid", "invoice.payment_succeeded"}:
        subscription = await _record_invoice(db, obj, paid=True)
        client_id = subscription.client_id if subscription else None

    elif event_type == "invoice.payment_failed":
        subscription = await _record_invoice(db, obj, paid=False)
        if subscription:
            client_id = subscription.client_id
            # Told immediately, with the retry date and a direct link to the
            # card form. Stripe will retry on its own schedule; the client has
            # until the last attempt to fix it, and only knows that if we say
            # so. This single email is the difference between recovering a
            # failed payment and losing the subscription to an expired card.
            await send_payment_failed(
                to=subscription.client.email,
                name=subscription.client.full_name.split()[0],
                amount_cents=obj.get("amount_due") or 0,
                currency=obj.get("currency") or settings.STRIPE_CURRENCY,
                retry_at=subscription.payment_retry_at,
                reason=subscription.last_payment_error,
            )

    elif event_type == "customer.subscription.trial_will_end":
        subscription = await _upsert_subscription(db, obj)
        client_id = subscription.client_id if subscription else None

    # Whatever happened, re-derive the cached level from the live subscription.
    if client_id is not None:
        level = await entitlements.sync_profile_level(db, client_id)
        log.info(
            "billing.entitlement_synced",
            user_id=str(client_id),
            stripe_event_type=event_type,
            level=level.value if level else None,
        )

    record.processed_at = datetime.now(UTC)
    await db.flush()
    return {"status": "processed"}