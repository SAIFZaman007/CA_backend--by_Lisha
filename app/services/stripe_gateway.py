"""Thin wrapper over the Stripe SDK.

Everything that talks to Stripe goes through here, for three reasons: the API
key is read in exactly one place, the rest of the codebase never imports
`stripe` directly, and the whole integration can be switched off with an empty
`STRIPE_SECRET_KEY` so the app still boots in development without credentials.

Prices are created lazily from the `programs` table rather than being managed by
hand in the Stripe dashboard. The coach edits a tier in the coach dashboard and
the correct Stripe price follows, so the two catalogues cannot drift.
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

        created = client.Customer.create(
            email=email, name=name, metadata={"user_id": user_id}
        )
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


async def create_billing_portal_session(*, customer_id: str, return_url: str) -> str:
    """Stripe's own portal for changing a card or cancelling.

    Cheaper and safer than rebuilding card management, and it stays correct as
    Stripe's requirements change.
    """
    client = _client()
    try:
        session = client.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
        return session.url
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


async def cancel_subscription(*, subscription_id: str, at_period_end: bool = True) -> dict:
    client = _client()
    try:
        if at_period_end:
            return client.Subscription.modify(subscription_id, cancel_at_period_end=True)
        return client.Subscription.cancel(subscription_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


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
        return stripe.Webhook.construct_event(
            payload, signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Malformed payload.") from exc
    except Exception as exc:  # noqa: BLE001 - stripe.SignatureVerificationError
        log.warning("stripe.bad_signature", error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid signature.") from exc