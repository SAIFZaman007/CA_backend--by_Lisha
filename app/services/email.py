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
from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings
from app.core.logging import get_logger
from app.services import email_templates as templates

log = get_logger("email")

_in_flight: set[asyncio.Task] = set()


async def send_email(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    reply_to: str | None = None,
) -> bool:
    """
    Deliver one message and wait for the result. Returns success.

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
    """
    Hand a message to the event loop and return at once.

    The caller gets control back before the SMTP connection is even opened, so
    a slow mail host costs the person waiting on the HTTP response nothing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(send_email(to, subject, text, html, reply_to=reply_to))
        return

    task = loop.create_task(send_email(to, subject, text, html, reply_to=reply_to))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


# --- The messages -------------------------------------------------------------

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
    queue_email(settings.COACH_EMAIL, subject, text, html, reply_to=email)