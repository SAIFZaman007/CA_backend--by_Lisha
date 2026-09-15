"""
Thin wrapper over the Stripe SDK.

Everything that talks to Stripe goes through here, for three reasons: the API
key is read in exactly one place, the rest of the codebase never imports
`stripe` directly, and the whole integration can be switched off with an empty
`STRIPE_SECRET_KEY` so the app still boots in development without credentials.

Prices are created lazily from the `programs` table rather than being managed by
hand in the Stripe dashboard. The coach edits a tier in the coach dashboard and
the correct Stripe price follows, so the two catalogues cannot drift.

Division of responsibility
--------------------------
This module performs Stripe operations and returns plain data. It does not
decide policy — whether a change counts as an upgrade, whether a downgrade
should be deferred, what a client is entitled to afterwards. That all lives in
`billing.py` and `entitlements.py`, where it can be read and changed without
anyone having to understand the Stripe SDK.

The one rule that is enforced here, because it is a Stripe mechanic rather than
a business rule: **proration behaviour is always explicit.** Stripe's default
for a subscription item change is `create_prorations`, which silently issues a
credit or a charge. Leaving that implicit is how a downgrade ends up refunding
money nobody intended to refund.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("stripe")

try:  # pragma: no cover - import guard
    import stripe
except ImportError:  # pragma: no cover
    stripe = None  # type: ignore[assignment]


def is_configured() -> bool:
    """Whether live calls are possible. False in a dev environment with no keys."""
    return bool(stripe and settings.STRIPE_SECRET_KEY)


def _client():
    if not is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payments are not configured on this environment yet.",
        )
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2024-06-20"
    return stripe


def _fail(exc: Exception) -> HTTPException:
    """Never surface Stripe's raw error text — it leaks internals and reads
    like machine output. Log the detail, show the person a sentence."""
    log.error("stripe.error", error=str(exc))
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail="The payment provider did not respond. Try again in a moment.",
    )


# --- Customers and prices -----------------------------------------------------


async def ensure_customer(*, email: str, name: str, user_id: str) -> str:
    """Find or create the Stripe customer for a user.

    `user_id` goes into metadata so a webhook can always map an event back to a
    local account even if the email has since changed.
    """
    client = _client()
    try:
        existing = client.Customer.search(query=f"metadata['user_id']:'{user_id}'", limit=1)
        if existing.data:
            return existing.data[0].id

        created = client.Customer.create(email=email, name=name, metadata={"user_id": user_id})
        return created.id
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def ensure_price(
    *, program_id: str, name: str, price_cents: int, currency: str, interval: str
) -> str:
    """Return a Stripe price id matching this tier, creating one if needed.

    Stripe prices are immutable, so a change of amount means a *new* price
    rather than an edit. Looking up by the amount plus the program id means a
    price change creates one new price and reuses it thereafter, instead of
    minting a fresh one on every checkout.
    """
    client = _client()
    lookup = f"program_{program_id}_{price_cents}_{currency}_{interval}"

    try:
        found = client.Price.list(lookup_keys=[lookup], limit=1)
        if found.data:
            return found.data[0].id

        product_search = client.Product.search(
            query=f"metadata['program_id']:'{program_id}'", limit=1
        )
        product_id = (
            product_search.data[0].id
            if product_search.data
            else client.Product.create(name=name, metadata={"program_id": program_id}).id
        )

        price = client.Price.create(
            product=product_id,
            unit_amount=price_cents,
            currency=currency,
            recurring=None if interval == "once" else {"interval": interval},
            lookup_key=lookup,
            metadata={"program_id": program_id},
        )
        return price.id
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# --- Checkout -----------------------------------------------------------------


async def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    user_id: str,
    program_id: str,
    mode: str,
    success_url: str,
    cancel_url: str,
) -> dict[str, Any]:
    """Open a Stripe Checkout session and return its id and URL.

    Card details never touch our servers — the client is redirected to Stripe
    and comes back with nothing more sensitive than a session id, which keeps
    this application firmly outside PCI scope.
    """
    client = _client()
    try:
        session = client.checkout.Session.create(
            customer=customer_id,
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            client_reference_id=user_id,
            # Repeated on the subscription too: `checkout.session.completed`
            # carries session metadata, later lifecycle events carry the
            # subscription's, and both paths need to resolve the tier.
            metadata={"user_id": user_id, "program_id": program_id},
            subscription_data=(
                {"metadata": {"user_id": user_id, "program_id": program_id}}
                if mode == "subscription"
                else None
            ),
        )
        return {"id": session.id, "url": session.url}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def retrieve_checkout_session(session_id: str):
    """Fetch a Checkout Session from Stripe.

    Used by the success-page endpoint to confirm a payment actually
    completed rather than trusting the redirect URL alone.
    """
    client = _client()
    try:
        return client.checkout.Session.retrieve(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# --- Reading a subscription ---------------------------------------------------


async def retrieve_subscription(subscription_id: str) -> dict[str, Any]:
    """The live subscription, with its item and price expanded.

    Expanding here rather than making a second call matters for a plan change:
    modifying a subscription requires the *item* id, not the subscription id,
    and fetching it separately doubles the latency of every upgrade.
    """
    client = _client()
    try:
        subscription = client.Subscription.retrieve(
            subscription_id, expand=["items.data.price", "default_payment_method"]
        )
        return subscription.to_dict() if hasattr(subscription, "to_dict") else dict(subscription)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def preview_plan_change(
    *, subscription_id: str, new_price_id: str
) -> dict[str, Any]:
    """What an immediate switch to `new_price_id` would cost, right now.

    Stripe calls this an upcoming-invoice preview. Showing it before the client
    confirms is the difference between "Upgrade" and "Upgrade — you will be
    charged $23.40 today, then $99 monthly from 14 March". A subscription
    change that bills an unexplained amount is the single most common source of
    payment disputes, and the preview costs one API call to avoid.

    Returns zeroes rather than raising if the preview is unavailable, because a
    missing preview should degrade the confirmation dialog, not block the
    upgrade.
    """
    client = _client()
    try:
        subscription = client.Subscription.retrieve(subscription_id)
        item_id = subscription["items"]["data"][0]["id"]

        invoice = client.Invoice.upcoming(
            customer=subscription["customer"],
            subscription=subscription_id,
            subscription_items=[{"id": item_id, "price": new_price_id, "quantity": 1}],
            subscription_proration_behavior="create_prorations",
        )
        return {
            "amount_due_cents": invoice.get("amount_due") or 0,
            "currency": invoice.get("currency") or settings.STRIPE_CURRENCY,
            "next_payment_attempt": invoice.get("next_payment_attempt"),
            "period_end": invoice.get("period_end"),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("stripe.preview_unavailable", error=str(exc))
        return {
            "amount_due_cents": None,
            "currency": settings.STRIPE_CURRENCY,
            "next_payment_attempt": None,
            "period_end": None,
        }


# --- Changing a plan ----------------------------------------------------------


async def change_subscription_price(
    *,
    subscription_id: str,
    new_price_id: str,
    program_id: str,
    prorate: bool,
) -> dict[str, Any]:
    """Move a live subscription onto a different price, effective immediately.

    Used for upgrades. `prorate=True` charges the difference for the remainder
    of the current period, which is what someone expects when they pay more to
    get more today.

    `payment_behavior="pending_if_incomplete"` is deliberate and load-bearing:
    if the proration charge needs authentication (3-D Secure) or the card is
    declined, the subscription stays on the *old* price rather than flipping to
    the new one and going unpaid. A failed upgrade must not be able to leave a
    client both downgraded and billed.

    The tier is written into metadata on the way through, because every later
    lifecycle event resolves the local program from subscription metadata — an
    upgrade that changes the price but not the metadata produces a subscription
    that bills for Level 3 and entitles Level 1.
    """
    client = _client()
    try:
        subscription = client.Subscription.retrieve(subscription_id)
        item_id = subscription["items"]["data"][0]["id"]
        metadata = dict(subscription.get("metadata") or {})
        metadata["program_id"] = program_id

        updated = client.Subscription.modify(
            subscription_id,
            items=[{"id": item_id, "price": new_price_id, "quantity": 1}],
            proration_behavior="create_prorations" if prorate else "none",
            payment_behavior="pending_if_incomplete",
            metadata=metadata,
            expand=["items.data.price"],
        )
        return updated.to_dict() if hasattr(updated, "to_dict") else dict(updated)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def schedule_price_change(
    *, subscription_id: str, new_price_id: str, program_id: str
) -> dict[str, Any]:
    """Queue a price change for the start of the next billing period.

    Used for downgrades. The client keeps the tier they have paid for until the
    period they paid for ends — removing features the moment someone clicks
    "Downgrade" is both unfair and, in practice, the fastest way to generate a
    refund request.

    Implemented with a Stripe subscription schedule rather than a local timer.
    Stripe owns the billing clock; anything that depends on our process being
    awake at the right moment is a change that eventually does not happen.

    `end_behavior="release"` returns the subscription to normal once the
    scheduled phase begins, so the schedule is a one-shot instruction and not a
    permanent structure to unwind later.
    """
    client = _client()
    try:
        subscription = client.Subscription.retrieve(subscription_id)
        current_price = subscription["items"]["data"][0]["price"]["id"]
        period_end = subscription["current_period_end"]
        metadata = dict(subscription.get("metadata") or {})

        schedule = client.SubscriptionSchedule.create(from_subscription=subscription_id)
        updated = client.SubscriptionSchedule.modify(
            schedule.id,
            end_behavior="release",
            phases=[
                # Phase one is what they already have, running out its clock.
                {
                    "items": [{"price": current_price, "quantity": 1}],
                    "start_date": subscription["current_period_start"],
                    "end_date": period_end,
                    "proration_behavior": "none",
                },
                # Phase two is the new tier, starting the instant the old one
                # ends. No proration: nothing is being changed mid-period.
                {
                    "items": [{"price": new_price_id, "quantity": 1}],
                    "start_date": period_end,
                    "proration_behavior": "none",
                    "metadata": {**metadata, "program_id": program_id},
                },
            ],
        )
        return {
            "schedule_id": schedule.id,
            "effective_at": period_end,
            "raw": updated.to_dict() if hasattr(updated, "to_dict") else dict(updated),
        }
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def release_schedule(schedule_id: str) -> None:
    """Drop a queued change. Used when a client cancels a pending downgrade.

    Releasing detaches the schedule and leaves the subscription exactly as it
    is — the correct outcome for "actually, keep me where I am". Failures are
    logged rather than raised: an orphaned schedule is untidy, but refusing the
    request because cleanup failed is worse.
    """
    client = _client()
    try:
        client.SubscriptionSchedule.release(schedule_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("stripe.schedule_release_failed", schedule_id=schedule_id, error=str(exc))


# --- Cancelling and resuming --------------------------------------------------


async def cancel_subscription(
    *, subscription_id: str, at_period_end: bool = True, reason: str | None = None
) -> dict:
    """Cancel, by default at the end of the paid period.

    The reason is written to Stripe's own cancellation_details as well as our
    database, so churn reporting in the Stripe dashboard matches what the coach
    sees in theirs.
    """
    client = _client()
    try:
        if at_period_end:
            return client.Subscription.modify(
                subscription_id,
                cancel_at_period_end=True,
                cancellation_details={"comment": reason} if reason else None,
            )
        return client.Subscription.cancel(subscription_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def resume_subscription(subscription_id: str) -> dict:
    """Undo a pending cancellation.

    Only meaningful while the subscription is still running — once Stripe has
    actually ended it there is nothing to resume and the client buys again.
    That asymmetry is why "Keep my plan" and "Subscribe" are different buttons
    in the portal.
    """
    client = _client()
    try:
        return client.Subscription.modify(subscription_id, cancel_at_period_end=False)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# --- Payment methods and invoices ---------------------------------------------


async def create_billing_portal_session(
    *, customer_id: str, return_url: str, flow: str | None = None
) -> str:
    """Stripe's own portal for cards, invoices and cancellation.

    Cheaper and safer than rebuilding card management, and it stays correct as
    Stripe's requirements change.

    `flow="payment_method_update"` deep-links straight to the card form rather
    than dropping the client on a menu. That matters most in the one case where
    it is used: a client who has just been told their payment failed should land
    on the field that fixes it, not on a page where they have to find it.
    """
    client = _client()
    try:
        params: dict[str, Any] = {"customer": customer_id, "return_url": return_url}
        if flow == "payment_method_update":
            params["flow_data"] = {
                "type": "payment_method_update",
                "after_completion": {"type": "redirect", "redirect": {"return_url": return_url}},
            }
        session = client.billing_portal.Session.create(**params)
        return session.url
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def get_payment_method(customer_id: str) -> dict[str, Any] | None:
    """Brand, last four and expiry of the card currently on file.

    Returns None rather than raising. A billing page that cannot render because
    the card lookup failed is worse than one that shows every other detail and
    omits the card.
    """
    client = _client()
    try:
        customer = client.Customer.retrieve(customer_id, expand=["invoice_settings.default_payment_method"])
        method = (customer.get("invoice_settings") or {}).get("default_payment_method")

        if not method:
            methods = client.PaymentMethod.list(customer=customer_id, type="card", limit=1)
            method = methods.data[0] if methods.data else None

        if not method:
            return None

        card = (method.get("card") if isinstance(method, dict) else method.card) or {}
        return {
            "brand": card.get("brand"),
            "last4": card.get("last4"),
            "exp": f"{card.get('exp_month'):02d}/{card.get('exp_year')}"
            if card.get("exp_month") and card.get("exp_year")
            else None,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("stripe.payment_method_unavailable", error=str(exc))
        return None


async def list_invoices(*, customer_id: str, limit: int = 24) -> list[dict[str, Any]]:
    """Invoices straight from Stripe, for reconciliation.

    Day-to-day billing history is served from the local `payments` table, which
    is faster and survives Stripe being unreachable. This exists for the cases
    where the local projection might be incomplete — a webhook that was missed
    while the service was down, or a coach checking why a client says they were
    charged and we have no record of it.
    """
    client = _client()
    try:
        invoices = client.Invoice.list(customer=customer_id, limit=limit)
        return [
            {
                "id": inv.get("id"),
                "number": inv.get("number"),
                "status": inv.get("status"),
                "amount_paid": inv.get("amount_paid"),
                "amount_due": inv.get("amount_due"),
                "currency": inv.get("currency"),
                "created": inv.get("created"),
                "hosted_invoice_url": inv.get("hosted_invoice_url"),
                "invoice_pdf": inv.get("invoice_pdf"),
            }
            for inv in invoices.data
        ]
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# --- Webhook ------------------------------------------------------------------


def verify_webhook(payload: bytes, signature: str) -> dict:
    """Verify a webhook's signature and return the event.

    This is the whole security boundary for billing: the endpoint is public, so
    an unsigned or badly-signed body must never be trusted. Anyone who could
    post arbitrary JSON here could otherwise grant themselves a subscription.
    """
    if not stripe:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payments are not configured."
        )
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret is not configured on this environment.",
        )

    try:
        return stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Malformed payload.") from exc
    except Exception as exc:  # noqa: BLE001 - stripe.SignatureVerificationError
        log.warning("stripe.bad_signature", error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid signature.") from exc