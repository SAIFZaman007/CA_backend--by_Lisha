"""Checkout, the Stripe webhook, and the entitlement the portal reads.

Flow, end to end:

    client picks a tier
        → POST /billing/checkout                 (we create a Stripe session)
        → Stripe Checkout                        (card details never reach us)
        → Stripe fires checkout.session.completed
        → POST /billing/webhook                  (we record the subscription)
        → profile.level is set                   (portal unlocks that tier)

The webhook, not the redirect, is what grants access. A client who closes the
tab on the success page is still subscribed; a client who forges a request to
the success URL is not.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.logging import get_logger
from app.models.billing import Payment, Subscription, WebhookEvent
from app.models.catalog import Program
from app.models.enums import PaymentStatus, SubscriptionStatus, UserRole
from app.models.user import User
from app.services import entitlements, stripe_gateway

router = APIRouter(prefix="/billing", tags=["billing"])
log = get_logger("billing")


def _dt(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value else None


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


@router.get("/history")
async def payment_history(user: CurrentUser, db: DbSession) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(Payment)
                .where(Payment.client_id == user.id)
                .order_by(Payment.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(p.id),
            "status": p.status.value,
            "amount_cents": p.amount_cents,
            "currency": p.currency,
            "description": p.description,
            "receipt_url": p.receipt_url,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "created_at": p.created_at.isoformat(),
        }
        for p in rows
    ]


# --- Buying --------------------------------------------------------------------


@router.post("/checkout")
async def start_checkout(payload: dict, user: CurrentUser, db: DbSession) -> dict:
    """Open a Stripe Checkout session for a tier."""
    if user.role is not UserRole.CLIENT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Coach accounts already have full access and cannot subscribe.",
        )

    try:
        program_id = uuid.UUID(str(payload.get("program_id")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Pick a plan.") from exc

    program = await db.get(Program, program_id)
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
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="You already have an active plan. Manage or change it from billing settings.",
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
        success_url=f"{settings.FRONTEND_URL}/portal/billing?checkout=success",
        cancel_url=f"{settings.FRONTEND_URL}/programmes/{program.slug}?checkout=cancelled",
    )

    log.info("billing.checkout_started", user_id=str(user.id), program_id=str(program.id))
    return session


@router.post("/portal")
async def billing_portal(user: CurrentUser, db: DbSession) -> dict:
    """Hand off to Stripe's own portal for cards, invoices and cancellation."""
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


@router.post("/cancel")
async def cancel(user: CurrentUser, db: DbSession) -> dict:
    """Cancel at the end of the paid period, never immediately.

    Someone who has paid to the end of the month keeps their programme to the
    end of the month. The webhook flips the status when Stripe actually ends it.
    """
    subscription = await entitlements.active_subscription(db, user.id)
    if subscription is None or not subscription.stripe_subscription_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="You have no active plan.")

    await stripe_gateway.cancel_subscription(
        subscription_id=subscription.stripe_subscription_id, at_period_end=True
    )
    subscription.cancel_at_period_end = True
    await db.flush()

    log.info("billing.cancel_requested", user_id=str(user.id))
    return {
        "status": "scheduled",
        "message": "Your plan stays active until the end of the period you have paid for.",
    }


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
    """
    if not stripe_signature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing signature.")

    event = stripe_gateway.verify_webhook(await request.body(), stripe_signature)
    event_id = event["id"]
    event_type = event["type"]

    # At-least-once delivery: the unique index is what actually stops a
    # duplicate `invoice.paid` from writing a second payment row.
    already = (
        (
            await db.execute(
                select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
            )
        )
        .scalars()
        .first()
    )
    if already is not None:
        return {"status": "duplicate"}

    record = WebhookEvent(stripe_event_id=event_id, event_type=event_type)
    db.add(record)
    await db.flush()

    obj = event["data"]["object"]
    client_id: uuid.UUID | None = None

    if event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        subscription = await _upsert_subscription(db, obj)
        if subscription:
            client_id = subscription.client_id

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

    elif event_type in {"invoice.paid", "invoice.payment_failed"}:
        paid = event_type == "invoice.paid"
        customer_id = obj.get("customer")

        subscription = (
            (
                await db.execute(
                    select(Subscription)
                    .where(Subscription.stripe_customer_id == customer_id)
                    .order_by(Subscription.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if subscription:
            client_id = subscription.client_id
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
                    description=obj.get("billing_reason"),
                    paid_at=datetime.now(UTC) if paid else None,
                )
            )
            await db.flush()

    # Whatever happened, re-derive the cached level from the live subscription.
    if client_id is not None:
        level = await entitlements.sync_profile_level(db, client_id)
        log.info(
            "billing.entitlement_synced",
            user_id=str(client_id),
            event=event_type,
            level=level.value if level else None,
        )

    record.processed_at = datetime.now(UTC)
    await db.flush()
    return {"status": "processed"}