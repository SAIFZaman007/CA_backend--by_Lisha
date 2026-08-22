"""Everything the marketing site needs, without a login."""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import DbSession, OptionalUser
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.models.catalog import Program, Testimonial
from app.models.engagement import ConsultationBooking, Lead
from app.schemas.catalog import ProgramOut, TestimonialOut
from app.schemas.tracking import BookingIn, BookingOut, LeadIn
from app.services import storage
from app.services.email import notify_coach_new_lead

router = APIRouter(tags=["public"])
log = get_logger("public")


@router.get("/meta/site")
async def site_meta() -> dict[str, str]:
    """Brand details the frontend renders in the header, footer and JSON-LD."""
    return {
        "brand": settings.BRAND_NAME,
        "business_name": settings.BUSINESS_NAME,
        "email": settings.SUPPORT_EMAIL,
        "instagram": settings.INSTAGRAM_URL,
        "site_url": settings.FRONTEND_URL,
    }


@router.get("/programs", response_model=list[ProgramOut])
async def list_programs(db: DbSession) -> list[Program]:
    result = await db.execute(
        select(Program).where(Program.is_active.is_(True)).order_by(Program.sort_order)
    )
    return list(result.scalars().all())


@router.get("/programs/{slug}", response_model=ProgramOut)
async def get_program(slug: str, db: DbSession) -> Program:
    program = (
        await db.execute(
            select(Program).where(Program.slug == slug, Program.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That programme does not exist.")
    return program


@router.get("/testimonials", response_model=list[TestimonialOut])
async def list_testimonials(db: DbSession) -> list[Testimonial]:
    result = await db.execute(
        select(Testimonial)
        .where(Testimonial.is_published.is_(True))
        .order_by(Testimonial.sort_order)
        .limit(12)
    )
    return list(result.scalars().all())


@router.post("/leads", status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_PUBLIC_FORM)
async def create_lead(
    request: Request, response: Response, payload: LeadIn, db: DbSession
) -> dict[str, str]:
    """The "Start your transformation" form."""
    if payload.website:
        # Honeypot tripped. Answer normally so the bot learns nothing.
        log.info("lead.honeypot_blocked")
        return {"message": "Thanks — your details are with Coach Auto."}

    db.add(
        Lead(
            full_name=payload.full_name.strip(),
            email=payload.email.lower().strip(),
            phone=payload.phone,
            level_interest=payload.level_interest,
            primary_goal=payload.primary_goal,
            message=payload.message,
            consent_marketing=payload.consent_marketing,
        )
    )
    await notify_coach_new_lead(payload.full_name, payload.email, payload.primary_goal)
    return {
        "message": "Thanks — your details are with Coach Auto. "
        "Expect a reply within one business day."
    }


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_PUBLIC_FORM)
async def create_booking(
    request: Request,
    response: Response,
    payload: BookingIn,
    db: DbSession,
    user: OptionalUser,
) -> ConsultationBooking:
    """Request a live chat slot. Works signed in or signed out."""
    booking = ConsultationBooking(
        client_id=user.id if user else None,
        name=payload.name.strip(),
        email=payload.email.lower().strip(),
        phone=payload.phone,
        preferred_at=payload.preferred_at,
        timezone=payload.timezone,
        topic=payload.topic,
    )
    db.add(booking)
    await db.flush()
    return booking


@router.get("/programs/{program_id}/image")
async def program_image(program_id: uuid.UUID, db: DbSession) -> FileResponse:
    """A tier's hero image.

    Public and unauthenticated on purpose — this is marketing artwork on the
    pricing page, not client data — so it is cached hard at the edge.
    """
    program = await db.get(Program, program_id)
    if program is None or not program.image_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No image for that plan.")

    return FileResponse(
        storage.resolve_path(program.image_key),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )