"""
What the coach can see about a client's money — and what she deliberately cannot.

The split matters. A coach needs to answer "has she paid?", "what is she on?",
"why did she leave?" and "can I send her a receipt?" without opening the Stripe
dashboard. She does not need, and should not have, the ability to change
someone else's subscription: a plan change moves real money, and the person
whose card it is has to be the one who authorises it. Every mutation in this
package is therefore read-only by design, and the one exception — a refund — is
routed to Stripe rather than reimplemented here, so it lands in the same audit
trail as everything else about that charge.

`CurrentCoach` reads, `CurrentAdmin` would be needed for anything destructive.
There is currently nothing destructive here, and that is the point.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.models.billing import Payment, Subscription
from app.models.catalog import Program
from app.models.enums import PaymentStatus, SubscriptionStatus
from app.models.user import User
from app.services import stripe_gateway

router = APIRouter(prefix="/billing")
log = get_logger("admin.billing")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


@router.get("/overview")
async def billing_overview(coach: CurrentCoach, db: DbSession) -> dict:
    """The numbers the coach actually runs the business on.

    Deliberately four figures rather than a dashboard. Monthly recurring
    revenue, who is paying, who is in trouble, and who is leaving — anything
    more becomes a screen nobody reads. Revenue is summed from live
    subscriptions rather than from historical payments, because the question
    being answered is "what comes in next month", not "what came in last".
    """
    active_rows = (
        (
            await db.execute(
                select(Subscription).where(
                    Subscription.status.in_(
                        [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING]
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    # Normalised to a monthly figure so annual and monthly plans can be added
    # together without quietly overstating the month they were sold in.
    mrr = sum(
        s.price_cents // 12 if s.billing_period == "year" else s.price_cents
        for s in active_rows
        if s.billing_period in ("month", "year")
    )

    past_due = (
        await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.PAST_DUE
            )
        )
    ).scalar_one()

    ending = (
        await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.cancel_at_period_end.is_(True),
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING]),
            )
        )
    ).scalar_one()

    failed_payments = (
        await db.execute(
            select(func.count(Payment.id)).where(Payment.status == PaymentStatus.FAILED)
        )
    ).scalar_one()

    return {
        "mrr_cents": mrr,
        "active_subscriptions": len(active_rows),
        "past_due": past_due,
        "ending_soon": ending,
        "failed_payments": failed_payments,
        "payments_configured": stripe_gateway.is_configured(),
    }


@router.get("/subscriptions")
async def list_subscriptions(
    coach: CurrentCoach,
    db: DbSession,
    status_filter: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Every subscription, filterable by the states worth acting on.

    "Attention" is the filter that earns its place: past due, or ending at the
    period boundary. Those are the two situations where a message from the
    coach changes the outcome, and they are invisible in a list sorted by name.
    """
    stmt = select(Subscription).order_by(Subscription.created_at.desc())

    if status_filter == "active":
        stmt = stmt.where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING])
        )
    elif status_filter == "attention":
        stmt = stmt.where(
            (Subscription.status == SubscriptionStatus.PAST_DUE)
            | (Subscription.cancel_at_period_end.is_(True))
        )
    elif status_filter == "cancelled":
        stmt = stmt.where(Subscription.status == SubscriptionStatus.CANCELED)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(s.id),
                "client_id": str(s.client_id),
                "client_name": s.client.full_name if s.client else None,
                "client_email": s.client.email if s.client else None,
                "program": s.program.name if s.program else None,
                "status": s.status.value,
                "price_cents": s.price_cents,
                "currency": s.currency,
                "billing_period": s.billing_period,
                "current_period_end": _iso(s.current_period_end),
                "cancel_at_period_end": s.cancel_at_period_end,
                "cancellation_reason": s.cancellation_reason,
                "cancellation_comment": s.cancellation_comment,
                "payment_failed_at": _iso(s.payment_failed_at),
                "last_payment_error": s.last_payment_error,
                "card": (
                    f"{s.payment_method_brand} ···· {s.payment_method_last4}"
                    if s.payment_method_last4
                    else None
                ),
            }
            for s in rows
        ],
    }


@router.get("/clients/{client_id}")
async def client_billing(client_id: uuid.UUID, coach: CurrentCoach, db: DbSession) -> dict:
    """One client's complete billing record, for the tab on their detail page.

    Returns an empty-but-valid shape for a client who has never paid, rather
    than a 404. "This client has no billing history" is a legitimate answer the
    screen should render, not an error it should handle.
    """
    client = await db.get(User, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That client was not found.")

    subscriptions = (
        (
            await db.execute(
                select(Subscription)
                .where(Subscription.client_id == client_id)
                .order_by(Subscription.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    payments = (
        (
            await db.execute(
                select(Payment)
                .where(Payment.client_id == client_id)
                .order_by(Payment.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )

    scheduled_names: dict[uuid.UUID, str] = {}
    pending_ids = [s.scheduled_program_id for s in subscriptions if s.scheduled_program_id]
    if pending_ids:
        for program in (
            (await db.execute(select(Program).where(Program.id.in_(pending_ids)))).scalars().all()
        ):
            scheduled_names[program.id] = program.name

    lifetime = sum(p.amount_cents for p in payments if p.status is PaymentStatus.SUCCEEDED)

    return {
        "client": {
            "id": str(client.id),
            "name": client.full_name,
            "email": client.email,
        },
        "lifetime_value_cents": lifetime,
        "subscriptions": [
            {
                "id": str(s.id),
                "program": s.program.name if s.program else None,
                "status": s.status.value,
                "price_cents": s.price_cents,
                "currency": s.currency,
                "billing_period": s.billing_period,
                "started_at": _iso(s.started_at),
                "current_period_end": _iso(s.current_period_end),
                "cancel_at_period_end": s.cancel_at_period_end,
                "canceled_at": _iso(s.canceled_at),
                "cancellation_reason": s.cancellation_reason,
                "cancellation_comment": s.cancellation_comment,
                "scheduled_change": (
                    {
                        "name": scheduled_names.get(s.scheduled_program_id),
                        "effective_at": _iso(s.scheduled_change_at),
                    }
                    if s.scheduled_program_id
                    else None
                ),
                "card": (
                    f"{s.payment_method_brand} ···· {s.payment_method_last4}"
                    if s.payment_method_last4
                    else None
                ),
                "last_payment_error": s.last_payment_error,
            }
            for s in subscriptions
        ],
        "payments": [
            {
                "id": str(p.id),
                "status": p.status.value,
                "amount_cents": p.amount_cents,
                "currency": p.currency,
                "invoice_number": p.invoice_number,
                "description": p.description,
                # Both links come straight from Stripe and are the only correct
                # source for a receipt. Generating a PDF here would produce a
                # second document that disagrees with the one the client was
                # emailed the moment tax or currency is involved.
                "receipt_url": p.receipt_url,
                "invoice_pdf_url": p.invoice_pdf_url,
                "failure_reason": p.failure_reason,
                "paid_at": _iso(p.paid_at),
                "created_at": _iso(p.created_at),
            }
            for p in payments
        ],
    }