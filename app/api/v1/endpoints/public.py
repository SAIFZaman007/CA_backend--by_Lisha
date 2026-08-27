"""Everything the marketing site needs, without a login.

Also home to the two files search engines fetch before anything else, the
sitemap and robots.txt. Both are generated here rather than shipped as static
files in the frontend build, because both need to list content that lives in
the database — every published coaching tier, every gallery image — and a
static file goes stale the moment the coach adds one.
"""

import uuid
from datetime import datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import DbSession, OptionalUser
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.models.catalog import Program, Testimonial
from app.models.engagement import ConsultationBooking, Lead
from app.models.gallery import GalleryImage
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


def _public_image_url(program: Program) -> str | None:
    """Whichever artwork the coach supplied: an upload wins over a pasted link.

    Mirrors `_image_url` in `app.api.v1.endpoints.admin.catalog` — kept as a
    separate function rather than a shared import because the two live in
    different routers with different purposes, but the resolution rule (and
    the route it points at) must stay identical between them, or one side
    shows artwork the other 404s on.
    """
    if program.image_key:
        return f"/api/v1/programs/{program.id}/image"
    return program.image_external_url


@router.get("/programs", response_model=list[ProgramOut])
async def list_programs(db: DbSession) -> list[ProgramOut]:
    result = await db.execute(
        select(Program).where(Program.is_active.is_(True)).order_by(Program.sort_order)
    )
    return [
        ProgramOut.model_validate(program).model_copy(
            update={"image_url": _public_image_url(program)}
        )
        for program in result.scalars().all()
    ]


@router.get("/programs/{slug}", response_model=ProgramOut)
async def get_program(slug: str, db: DbSession) -> ProgramOut:
    program = (
        await db.execute(
            select(Program).where(Program.slug == slug, Program.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That programme does not exist.")
    return ProgramOut.model_validate(program).model_copy(
        update={"image_url": _public_image_url(program)}
    )


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


# --- Search engines -----------------------------------------------------------
#
# Generated, not static. Both files have to list database-backed content — the
# coaching tiers and the gallery — and a file baked into the frontend build
# goes stale the first time the coach publishes a photo.

# Crawl priority is relative, not absolute: it tells a crawler which of *our*
# pages matter most, nothing about how we rank against anyone else. The
# homepage and the programme pages convert; the legal pages exist because they
# have to.
_STATIC_ROUTES: list[tuple[str, str, str]] = [
    ("/", "weekly", "1.0"),
    ("/programs", "weekly", "0.9"),
    ("/gallery", "weekly", "0.8"),
    ("/about", "monthly", "0.7"),
    ("/tools", "monthly", "0.7"),
    ("/contact", "monthly", "0.6"),
    ("/privacy", "yearly", "0.2"),
    ("/terms", "yearly", "0.2"),
]


def _url_entry(
    loc: str, *, lastmod: datetime | None, changefreq: str, priority: str
) -> str:
    parts = [f"    <loc>{escape(loc)}</loc>"]
    if lastmod is not None:
        # W3C datetime, which is what the sitemap protocol asks for. A bare
        # date is legal too, but a timestamp lets a crawler tell a photo
        # published this morning from one published three weeks ago.
        parts.append(f"    <lastmod>{lastmod.date().isoformat()}</lastmod>")
    parts.append(f"    <changefreq>{changefreq}</changefreq>")
    parts.append(f"    <priority>{priority}</priority>")
    body = "\n".join(parts)
    return f"  <url>\n{body}\n  </url>"


@router.get("/meta/sitemap.xml", include_in_schema=False)
async def sitemap(db: DbSession) -> Response:
    """The sitemap, built from what is actually published right now.

    Served through nginx at `/sitemap.xml` — see the frontend's nginx.conf. It
    lives under the API because only the API knows which tiers are live and
    which photos are published, and listing an unpublished URL is how a site
    ends up with soft-404s in Search Console.
    """
    origin = settings.canonical_origin

    entries = [
        _url_entry(f"{origin}{path}", lastmod=None, changefreq=freq, priority=priority)
        for path, freq, priority in _STATIC_ROUTES
    ]

    programs = (
        (
            await db.execute(
                select(Program)
                .where(Program.is_active.is_(True))
                .order_by(Program.sort_order)
            )
        )
        .scalars()
        .all()
    )
    entries.extend(
        _url_entry(
            f"{origin}/programs/{program.slug}",
            lastmod=program.updated_at,
            changefreq="weekly",
            priority="0.9",
        )
        for program in programs
    )

    # Image sitemap extension. Gallery photos are the one part of this site
    # with real Google Images potential, and the `<image:>` namespace is how
    # they get indexed with their captions attached rather than as anonymous
    # files behind an API path.
    images = (
        (
            await db.execute(
                select(GalleryImage)
                .where(GalleryImage.is_published.is_(True))
                .order_by(GalleryImage.sort_order)
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )
    if images:
        image_nodes = "\n".join(
            "    <image:image>\n"
            f"      <image:loc>{escape(origin)}{settings.API_V1_PREFIX}/gallery/{image.id}/file</image:loc>\n"
            f"      <image:title>{escape(image.title)}</image:title>\n"
            f"      <image:caption>{escape(image.alt_text)}</image:caption>\n"
            "    </image:image>"
            for image in images
        )
        entries.append(
            f"  <url>\n    <loc>{escape(origin)}/gallery</loc>\n"
            f"    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n"
            f"{image_nodes}\n  </url>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/meta/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    """robots.txt, with the sitemap pointed at the canonical origin.

    The portal is disallowed in full. Everything behind `/portal` is personal
    health data — weights, measurements, photos — and while it all sits behind
    authentication anyway, telling a crawler not to try is the belt to that
    braces.

    Answer engines get an explicit allow on the public pages. That is a
    deliberate choice, not an oversight: the programme and calculator
    explanations are written to be quoted, and being the source an assistant
    cites is worth more to an online coaching business than the pageview it
    replaces.
    """
    origin = settings.canonical_origin
    body = f"""# {settings.BRAND_NAME} — {settings.BUSINESS_NAME}
User-agent: *
Allow: /

# The client portal and account screens hold personal health data.
Disallow: /portal
Disallow: /login
Disallow: /register
Disallow: /forgot-password
Disallow: /reset-password
Disallow: /api/

# Answer engines are welcome on the public pages.
User-agent: GPTBot
Allow: /
Disallow: /portal

User-agent: PerplexityBot
Allow: /
Disallow: /portal

User-agent: ClaudeBot
Allow: /
Disallow: /portal

Sitemap: {origin}/sitemap.xml
"""
    return PlainTextResponse(
        body, headers={"Cache-Control": "public, max-age=86400"}
    )