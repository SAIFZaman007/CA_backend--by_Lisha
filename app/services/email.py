"""Transactional email. Falls back to logging when SMTP is not configured,
so local development never needs a mail server."""

from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("email")


async def send_email(to: str, subject: str, body: str) -> bool:
    if not settings.SMTP_HOST:
        log.info("email.skipped_no_smtp", to=to, subject=subject, body=body)
        return False

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

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


async def send_password_reset(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    await send_email(
        to,
        "Reset your Coach Auto password",
        (
            "You asked to reset your Coach Auto password.\n\n"
            f"Open this link to set a new one:\n{link}\n\n"
            f"The link works for {settings.PASSWORD_RESET_EXPIRE_MINUTES} minutes. "
            "If you did not ask for this, you can ignore this email — nothing has changed.\n\n"
            "— Coach Auto | Autonomy Health and Fitness"
        ),
    )


async def send_welcome(to: str, name: str) -> None:
    await send_email(
        to,
        "Welcome to Coach Auto",
        (
            f"Hi {name},\n\n"
            "Your Coach Auto account is ready. Sign in to complete your intake — height, "
            "weight, tape measurements and your starting photos — so your coach can build "
            "your first block of training.\n\n"
            f"{settings.FRONTEND_URL}/login\n\n"
            "— Coach Auto | Autonomy Health and Fitness"
        ),
    )


async def notify_coach_new_lead(name: str, email: str, goal: str | None) -> None:
    await send_email(
        settings.COACH_EMAIL,
        f"New enquiry from {name}",
        f"Name: {name}\nEmail: {email}\nGoal: {goal or 'not given'}\n",
    )
