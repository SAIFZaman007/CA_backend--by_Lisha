"""
Transactional email.

Two things changed here and both matter in production.

**Messages are now multipart/alternative.** Every send carries a plain-text
part and a branded HTML part, and the mail client picks. Previously only text
went out, which is why a welcome email from a coaching business arrived looking
like a server log. The HTML lives in `email_templates.py`; this module is only
the transport.

**Sending no longer blocks the request.** `send_welcome` used to be awaited
inline inside `POST /auth/register`, so the client sat on a spinner for as long
as the SMTP handshake took — and if the mail host was slow, for the full
15-second timeout. Worse, a hung SMTP connection held an open database
transaction the whole time. Sends are now handed to the event loop and the
request returns immediately. Nothing downstream depends on the result: the API
contract for "forgot password" is deliberately identical whether or not the
address exists, so there was never a delivery outcome worth waiting for.

Failures are logged, never raised. An email that does not arrive must not turn
a successful registration into a 500 the client sees.

With no SMTP host configured the whole thing degrades to a log line, so a
fresh clone runs with no mail server.
"""

import asyncio
from datetime import datetime
from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings
from app.core.logging import get_logger
from app.services import email_templates as templates

log = get_logger("email")

# `asyncio.create_task` keeps only a weak reference to the task it returns, so
# a task nobody holds can be garbage collected mid-flight and the email
# silently never sends. Holding a strong reference until completion is the
# documented way to avoid that.
_in_flight: set[asyncio.Task] = set()


async def send_email(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    reply_to: str | None = None,
) -> bool:
    """Deliver one message and wait for the result. Returns success.

    Prefer `queue_email` from inside a request handler — this is the blocking
    form, kept for tests, the CLI, and anywhere the outcome is actually needed.
    """
    if not settings.SMTP_HOST:
        log.info("email.skipped_no_smtp", to=to, subject=subject)
        return False

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message["Reply-To"] = reply_to or settings.email_reply_to

    # Order is significant: `set_content` writes the plain-text body, then
    # `add_alternative` attaches the HTML as the richer option. A client that
    # understands HTML shows the second part, a text-only client shows the
    # first. Reversing these two lines sends HTML source as the fallback.
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=settings.SMTP_STARTTLS,
            timeout=15,
        )
        log.info("email.sent", to=to, subject=subject)
        return True
    except Exception as exc:  # noqa: BLE001 — never let mail failures break a request
        log.error("email.failed", to=to, subject=subject, error=str(exc))
        return False


def queue_email(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    reply_to: str | None = None,
) -> None:
    """Hand a message to the event loop and return at once.

    The caller gets control back before the SMTP connection is even opened, so
    a slow mail host costs the person waiting on the HTTP response nothing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop — a synchronous script or a test. Fall back to a blocking
        # send rather than dropping the message on the floor.
        asyncio.run(send_email(to, subject, text, html, reply_to=reply_to))
        return

    task = loop.create_task(send_email(to, subject, text, html, reply_to=reply_to))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


# --- The messages -------------------------------------------------------------
#
# Signatures are unchanged from the previous version, so no call site needed
# editing. They still return awaitables; they simply return almost instantly
# now instead of waiting on SMTP.


async def send_password_reset(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}"
    subject, text, html = templates.password_reset(
        link, settings.PASSWORD_RESET_EXPIRE_MINUTES
    )
    queue_email(to, subject, text, html)


async def send_welcome(to: str, name: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/login"
    subject, text, html = templates.welcome(name, link)
    queue_email(to, subject, text, html)


async def notify_coach_new_lead(
    name: str, email: str, goal: str | None, phone: str | None = None
) -> None:
    subject, text, html = templates.coach_new_lead(name, email, goal, phone)
    # Reply-To is the enquirer, so the coach can answer straight from the
    # notification instead of copying an address out of the body.
    queue_email(settings.COACH_EMAIL, subject, text, html, reply_to=email)


def _money(amount_cents: int, currency: str) -> str:
    """A number a person reads, not a number a ledger stores."""
    symbol = {"usd": "$", "gbp": "\u00a3", "eur": "\u20ac"}.get(currency.lower(), "")
    return f"{symbol}{amount_cents / 100:,.2f}" if symbol else f"{amount_cents / 100:,.2f} {currency.upper()}"


async def send_payment_failed(
    *,
    to: str,
    name: str,
    amount_cents: int,
    currency: str,
    retry_at: datetime | None,
    reason: str | None,
) -> None:
    """Tell a client their renewal failed, with a deadline and a fix.

    The retry date is included whenever Stripe supplies one. "We will try again
    on the 14th" turns a vague worry into a task with a due date, and it is the
    single line that most reliably recovers a failed payment.
    """
    link = f"{settings.FRONTEND_URL.rstrip('/')}/portal/billing"
    if retry_at:
        retry_text = (
            f"We will automatically try again on {retry_at.strftime('%-d %B')}. "
            "Updating your card before then means you will not notice this happened."
        )
    else:
        retry_text = (
            "Update your card to keep your plan running — we will retry as soon as you do."
        )

    subject, text, html = templates.payment_failed(
        name, _money(amount_cents, currency), retry_text, reason, link
    )
    queue_email(to, subject, text, html)


async def send_subscription_cancelled(*, to: str, name: str, program_name: str) -> None:
    """Confirm a plan has actually ended. Sent by the webhook, not the request."""
    link = f"{settings.FRONTEND_URL.rstrip('/')}/programs"
    subject, text, html = templates.subscription_cancelled(name, program_name, link)
    queue_email(to, subject, text, html)