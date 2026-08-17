"""Coach ↔ client messaging.

A client has exactly one thread with their coach. Clients who want a live call
book a slot through the consultation endpoint instead.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.models.engagement import Message, MessageThread
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.tracking import MessageIn, MessageOut, ThreadOut

router = APIRouter(prefix="/messages", tags=["messages"])


async def _coach(db: DbSession) -> User:
    coach = (
        await db.execute(
            select(User).where(User.role == UserRole.COACH).order_by(User.created_at).limit(1)
        )
    ).scalar_one_or_none()
    if coach is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Messaging is not set up yet. Email {settings.SUPPORT_EMAIL} in the meantime.",
        )
    return coach


async def _thread_for(db: DbSession, user: User) -> MessageThread:
    coach = await _coach(db)
    if user.role in (UserRole.COACH, UserRole.ADMIN):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Coaches open client threads from the coach dashboard.",
        )

    stmt = (
        select(MessageThread)
        .where(MessageThread.client_id == user.id)
        .options(selectinload(MessageThread.messages))
    )
    thread = (await db.execute(stmt)).scalars().first()
    if thread:
        return thread

    thread = MessageThread(client_id=user.id, coach_id=coach.id)
    db.add(thread)
    await db.flush()
    await db.refresh(thread, ["messages"])
    return thread


@router.get("/thread", response_model=ThreadOut)
async def my_thread(user: CurrentUser, db: DbSession) -> MessageThread:
    """The client's conversation with Coach Auto. Created on first open."""
    thread = await _thread_for(db, user)

    # Anything the coach sent is now read.
    for message in thread.messages:
        if message.sender_id != user.id and message.read_at is None:
            message.read_at = datetime.now(UTC)

    return thread


@router.post("/thread", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_message(payload: MessageIn, user: CurrentUser, db: DbSession) -> Message:
    thread = await _thread_for(db, user)
    message = Message(thread_id=thread.id, sender_id=user.id, body=payload.body.strip())
    db.add(message)
    thread.last_message_at = datetime.now(UTC)
    await db.flush()
    return message


@router.get("/unread-count")
async def unread_count(user: CurrentUser, db: DbSession) -> dict[str, int]:
    count = (
        await db.execute(
            select(func.count(Message.id))
            .join(MessageThread)
            .where(
                MessageThread.client_id == user.id,
                Message.sender_id != user.id,
                Message.read_at.is_(None),
            )
        )
    ).scalar_one()
    return {"unread": count}
