"""Everything waiting for a reply: client threads, website enquiries, bookings."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.api.v1.endpoints.messages import serialise_attachment
from app.models.engagement import ConsultationBooking, Lead, Message, MessageThread
from app.models.enums import BookingStatus, LeadStatus
from app.models.user import User
from app.schemas.admin import (
    BookingOut,
    ThreadAttachment,
    BookingUpdate,
    LeadOut,
    LeadUpdate,
    MessageIn,
    ThreadMessage,
    ThreadOut,
    ThreadSummary,
)

router = APIRouter()
log = get_logger("admin.inbox")

PREVIEW_CHARS = 120


# --- Threads ------------------------------------------------------------------


@router.get("/unread-count")
async def unread_count(coach: CurrentCoach, db: DbSession) -> dict[str, int]:
    """How many client messages are waiting on a reply.

    Its own tiny endpoint so the sidebar badge can poll every few seconds
    without dragging the whole overview payload — counts and dashboards want
    very different refresh rates.
    """
    total = (
        await db.execute(
            select(func.count(Message.id))
            .join(MessageThread, MessageThread.id == Message.thread_id)
            .where(Message.read_at.is_(None), Message.sender_id == MessageThread.client_id)
        )
    ).scalar_one()

    threads = (
        await db.execute(
            select(func.count(func.distinct(Message.thread_id)))
            .join(MessageThread, MessageThread.id == Message.thread_id)
            .where(Message.read_at.is_(None), Message.sender_id == MessageThread.client_id)
        )
    ).scalar_one()

    return {"unread": total, "threads": threads}


@router.get("/threads", response_model=list[ThreadSummary])
async def list_threads(
    coach: CurrentCoach,
    db: DbSession,
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
) -> list[ThreadSummary]:
    unread = (
        select(Message.thread_id.label("tid"), func.count(Message.id).label("n"))
        .join(MessageThread, MessageThread.id == Message.thread_id)
        .where(Message.read_at.is_(None), Message.sender_id == MessageThread.client_id)
        .group_by(Message.thread_id)
        .subquery()
    )

    stmt = (
        select(MessageThread, User, func.coalesce(unread.c.n, 0))
        .join(User, User.id == MessageThread.client_id)
        .outerjoin(unread, unread.c.tid == MessageThread.id)
        .where(MessageThread.is_archived.is_(False))
        .order_by(MessageThread.last_message_at.desc().nullslast())
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(unread.c.n > 0)

    rows = (await db.execute(stmt)).all()

    summaries: list[ThreadSummary] = []
    for thread, client, unread_count in rows:
        last = thread.messages[-1] if thread.messages else None
        summaries.append(
            ThreadSummary(
                # An image-only message has an empty body, so a preview line
                # alone would render as blank and read as "nothing here". The
                # flag is what puts a paperclip on the row instead.
                has_attachments=bool(last and last.attachments),
                thread_id=thread.id,
                client_id=client.id,
                client_name=client.display_name or client.full_name,
                avatar_url=client.avatar_url,
                subject=thread.subject,
                last_message_at=thread.last_message_at,
                preview=(last.body[:PREVIEW_CHARS] if last else None),
                unread=unread_count,
            )
        )
    return summaries


async def _thread_for_client(db: DbSession, client_id: uuid.UUID, coach_id: uuid.UUID):
    thread = (
        await db.execute(select(MessageThread).where(MessageThread.client_id == client_id))
    ).scalar_one_or_none()

    if thread is None:
        client = await db.get(User, client_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That client was not found.")
        thread = MessageThread(client_id=client_id, coach_id=coach_id)
        db.add(thread)
        await db.flush()
        await db.refresh(thread, ["messages"])
    return thread


@router.get("/clients/{client_id}/thread", response_model=ThreadOut)
async def read_thread(
    client_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> ThreadOut:
    """Opening a thread marks the client's messages as read — that is what the
    unread badge means, and it should not need a second click."""
    client = await db.get(User, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That client was not found.")

    thread = await _thread_for_client(db, client_id, coach.id)
    now = datetime.now(UTC)

    for message in thread.messages:
        if message.sender_id == client_id and message.read_at is None:
            message.read_at = now
    await db.flush()

    return ThreadOut(
        thread_id=thread.id,
        client_id=client_id,
        client_name=client.display_name or client.full_name,
        subject=thread.subject,
        messages=[
            ThreadMessage(
                id=m.id,
                body=m.body,
                created_at=m.created_at,
                read_at=m.read_at,
                from_coach=m.sender_id != client_id,
                attachments=[
                    ThreadAttachment(
                        **serialise_attachment(attachment, coach.id).model_dump(
                            exclude={"kind"}
                        )
                    )
                    for attachment in m.attachments
                ],
            )
            for m in thread.messages
        ],
    )


@router.post(
    "/clients/{client_id}/thread",
    response_model=ThreadMessage,
    status_code=status.HTTP_201_CREATED,
)
async def reply(
    client_id: uuid.UUID, payload: MessageIn, coach: CurrentCoach, db: DbSession
) -> ThreadMessage:
    thread = await _thread_for_client(db, client_id, coach.id)

    message = Message(thread_id=thread.id, sender_id=coach.id, body=payload.body.strip())
    db.add(message)
    thread.last_message_at = datetime.now(UTC)
    await db.flush()

    log.info("admin.reply_sent", client_id=str(client_id), by=str(coach.id))
    return ThreadMessage(
        id=message.id,
        body=message.body,
        created_at=message.created_at,
        read_at=None,
        from_coach=True,
        attachments=[],
    )


# --- Leads --------------------------------------------------------------------


@router.get("/leads", response_model=list[LeadOut])
async def list_leads(
    coach: CurrentCoach,
    db: DbSession,
    status_filter: LeadStatus | None = None,
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> list[Lead]:
    stmt = select(Lead).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        stmt = stmt.where(Lead.status == status_filter)
    return list((await db.execute(stmt)).scalars().all())


@router.patch("/leads/{lead_id}", response_model=LeadOut)
async def update_lead(
    lead_id: uuid.UUID, payload: LeadUpdate, coach: CurrentCoach, db: DbSession
) -> Lead:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That enquiry was not found.")
    lead.status = payload.status
    await db.flush()
    return lead


@router.delete("/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(lead_id: uuid.UUID, coach: CurrentCoach, db: DbSession) -> None:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That enquiry was not found.")
    await db.delete(lead)


# --- Consultation bookings ----------------------------------------------------


@router.get("/bookings", response_model=list[BookingOut])
async def list_bookings(
    coach: CurrentCoach,
    db: DbSession,
    status_filter: BookingStatus | None = None,
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> list[ConsultationBooking]:
    stmt = (
        select(ConsultationBooking)
        .order_by(ConsultationBooking.preferred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status_filter:
        stmt = stmt.where(ConsultationBooking.status == status_filter)
    return list((await db.execute(stmt)).scalars().all())


@router.patch("/bookings/{booking_id}", response_model=BookingOut)
async def update_booking(
    booking_id: uuid.UUID, payload: BookingUpdate, coach: CurrentCoach, db: DbSession
) -> ConsultationBooking:
    booking = await db.get(ConsultationBooking, booking_id)
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That booking was not found.")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(booking, field, value)
    await db.flush()
    return booking


@router.delete("/bookings/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_booking(
    booking_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> None:
    booking = await db.get(ConsultationBooking, booking_id)
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That booking was not found.")
    await db.delete(booking)