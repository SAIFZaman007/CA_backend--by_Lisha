"""Coach ↔ client messaging.

A client has exactly one thread with their coach. Clients who want a live call
book a slot through the consultation endpoint instead.

**Attachments.** A client can send images — a loaded bar, a meal, their setup
mid-set — so a form question does not need three paragraphs of description.
Bytes go up on their own request first and the message that references them is
written second. Two reasons: a slow upload does not block the text box, and a
validation failure on the message never costs anyone a re-upload.

The images are private client data and are served exactly as check-in photos
are: never as static files, only through a short-lived signature bound to one
attachment and one viewer.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, OptionalUser
from app.core.logging import get_logger
from app.core.security import sign_media_url, verify_media_token
from app.models.engagement import Message, MessageAttachment, MessageThread
from app.models.enums import AttachmentKind, UserRole
from app.models.user import User
from app.schemas.tracking import (
    AttachmentOut,
    AttachmentUploadOut,
    MessageIn,
    MessageOut,
    ThreadOut,
)
from app.services import storage

router = APIRouter(prefix="/messages", tags=["messages"])
log = get_logger("messages")


# --- Participants -------------------------------------------------------------


async def _coach(db: DbSession) -> User:
    # The business runs on a single coach who is also the super admin, so that
    # account's role is ADMIN. Matching only COACH found nobody and every client
    # thread failed — this looks for either, oldest first.
    coach = (
        await db.execute(
            select(User)
            .where(User.role.in_((UserRole.COACH, UserRole.ADMIN)), User.is_active.is_(True))
            .order_by(User.role == UserRole.COACH, User.created_at)
            .limit(1)
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
        .options(selectinload(MessageThread.messages).selectinload(Message.attachments))
    )
    thread = (await db.execute(stmt)).scalars().first()
    if thread:
        return thread

    thread = MessageThread(client_id=user.id, coach_id=coach.id)
    db.add(thread)
    await db.flush()
    await db.refresh(thread, ["messages"])
    return thread


# --- Attachment URLs ----------------------------------------------------------


def attachment_url(attachment: MessageAttachment, viewer_id: uuid.UUID) -> str:
    """A URL an <img> tag can actually load.

    The file endpoint accepts either a bearer token or this signature, so the
    same route serves a scripted fetch and a plain image tag. The signature is
    bound to this one attachment: without that binding, a token minted for one
    photo would open every photo the same viewer could reach.
    """
    token = sign_media_url(attachment.id, viewer_id, settings.MEDIA_URL_TTL_SECONDS)
    return f"{settings.API_V1_PREFIX}/messages/attachments/{attachment.id}/file?token={token}"


def serialise_attachment(
    attachment: MessageAttachment, viewer_id: uuid.UUID
) -> AttachmentOut:
    return AttachmentOut(
        id=attachment.id,
        kind=attachment.kind,
        url=attachment_url(attachment, viewer_id),
        content_type=attachment.content_type,
        file_size_bytes=attachment.file_size_bytes,
        width=attachment.width,
        height=attachment.height,
        original_name=attachment.original_name,
    )


def serialise_message(message: Message, viewer_id: uuid.UUID) -> MessageOut:
    return MessageOut(
        id=message.id,
        sender_id=message.sender_id,
        body=message.body,
        read_at=message.read_at,
        created_at=message.created_at,
        attachments=[
            serialise_attachment(attachment, viewer_id) for attachment in message.attachments
        ],
    )


# --- Reading ------------------------------------------------------------------


@router.get("/thread", response_model=ThreadOut)
async def my_thread(user: CurrentUser, db: DbSession) -> ThreadOut:
    """The client's conversation with Coach Auto. Created on first open."""
    thread = await _thread_for(db, user)

    # Anything the coach sent is now read.
    for message in thread.messages:
        if message.sender_id != user.id and message.read_at is None:
            message.read_at = datetime.now(UTC)

    return ThreadOut(
        id=thread.id,
        subject=thread.subject,
        last_message_at=thread.last_message_at,
        messages=[serialise_message(message, user.id) for message in thread.messages],
    )


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


# --- Attachments --------------------------------------------------------------


async def _purge_orphans(db: DbSession, owner_id: uuid.UUID) -> None:
    """Delete this uploader's stale unbound attachments.

    Swept opportunistically on each upload rather than by a scheduled job. The
    platform runs a single worker with no task queue, and the only account that
    can create orphans is the one uploading right now — so the cheapest correct
    place to clean up is here, bounded to their own rows.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=settings.ORPHAN_ATTACHMENT_TTL_HOURS)
    stale = (
        (
            await db.execute(
                select(MessageAttachment).where(
                    MessageAttachment.uploaded_by_id == owner_id,
                    MessageAttachment.message_id.is_(None),
                    MessageAttachment.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for attachment in stale:
        storage.delete_file(attachment.file_key)
        await db.delete(attachment)


@router.post(
    "/attachments",
    response_model=AttachmentUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = File(...),
) -> AttachmentUploadOut:
    """Upload one image, unattached. Send it with the next message."""
    if user.role in (UserRole.COACH, UserRole.ADMIN):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Coaches attach images from the coach dashboard.",
        )

    await _purge_orphans(db, user.id)

    key, content_type, size, width, height = await storage.save_message_image(user.id, file)

    attachment = MessageAttachment(
        message_id=None,
        uploaded_by_id=user.id,
        kind=AttachmentKind.IMAGE,
        file_key=key,
        content_type=content_type,
        file_size_bytes=size,
        width=width,
        height=height,
        # Recorded for display only. Never used to build a path — see
        # `storage.save_message_image`.
        original_name=(file.filename or "")[:200] or None,
    )
    db.add(attachment)
    await db.flush()

    log.info("message.attachment_uploaded", user_id=str(user.id), bytes=size)
    return AttachmentUploadOut(
        id=attachment.id,
        url=attachment_url(attachment, user.id),
        content_type=content_type,
        file_size_bytes=size,
        width=width,
        height=height,
        original_name=attachment.original_name,
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_attachment(
    attachment_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    """Remove an attachment the client picked and then thought better of.

    Only while it is still unbound. Once it is part of a sent message it is
    part of the conversation history, and deleting half a message the coach has
    already read is not a tidy-up, it is a rewrite.
    """
    attachment = await db.get(MessageAttachment, attachment_id)
    if attachment is None or attachment.uploaded_by_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That image was not found.")
    if attachment.message_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="That image has already been sent and cannot be removed.",
        )

    storage.delete_file(attachment.file_key)
    await db.delete(attachment)


@router.get("/attachments/{attachment_id}/file")
async def attachment_file(
    attachment_id: uuid.UUID,
    db: DbSession,
    user: OptionalUser,
    token: str | None = Query(None),
) -> Response:
    """Serve one attachment to someone entitled to see it.

    Two ways to identify the viewer, because two different callers need it: a
    signed `token` for an <img> tag, which cannot send an Authorization header,
    and a bearer token for a scripted fetch. Either way, identifying the viewer
    and authorising them are separate steps — the signature proves who asked,
    not what they are allowed to see.
    """
    attachment = await db.get(MessageAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Image not found.")

    viewer_id: uuid.UUID | None = verify_media_token(token, attachment_id) if token else None
    if viewer_id is None and user is not None:
        viewer_id = user.id
    if viewer_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="That link has expired.")

    if attachment.message_id is None:
        # Still unsent. Visible to whoever uploaded it and nobody else — this is
        # the preview in the client's own composer.
        permitted = attachment.uploaded_by_id == viewer_id
    else:
        # Sent. Visible to both sides of the conversation it landed in.
        #
        # Resolving the thread rather than trusting the signature is what stops
        # a leaked link outliving the relationship it was issued inside: when
        # the thread goes, so does the access.
        permitted = bool(
            (
                await db.execute(
                    select(func.count(Message.id))
                    .join(MessageThread, MessageThread.id == Message.thread_id)
                    .where(
                        Message.id == attachment.message_id,
                        or_(
                            MessageThread.client_id == viewer_id,
                            MessageThread.coach_id == viewer_id,
                        ),
                    )
                )
            ).scalar_one()
        )

    if not permitted:
        # 404 rather than 403: confirming an image exists but is off-limits
        # tells an enumerating caller which ids are real.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Image not found.")

    return FileResponse(
        storage.resolve_path(attachment.file_key),
        media_type=attachment.content_type,
        # Private, and short. Long enough that scrolling a conversation does not
        # refetch every photo, short enough that a shared browser does not hold
        # someone's progress shots in cache all afternoon.
        headers={"Cache-Control": "private, max-age=300"},
    )


# --- Sending ------------------------------------------------------------------


async def claim_attachments(
    db: DbSession,
    *,
    attachment_ids: list[uuid.UUID],
    owner_id: uuid.UUID,
    message_id: uuid.UUID,
) -> list[MessageAttachment]:
    """Bind uploaded images to a message, verifying ownership first.

    Two checks, both load-bearing. `uploaded_by_id` is the only thing standing
    between one client and another client's upload. `message_id IS NULL` stops
    an attachment being re-pointed at a second message, which would otherwise
    let someone move an image out of a conversation the coach had already read.
    """
    if not attachment_ids:
        return []

    rows = (
        (
            await db.execute(
                select(MessageAttachment).where(
                    MessageAttachment.id.in_(attachment_ids),
                    MessageAttachment.uploaded_by_id == owner_id,
                    MessageAttachment.message_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != len(set(attachment_ids)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One of those images is no longer available. Attach it again and resend.",
        )

    for attachment in rows:
        attachment.message_id = message_id
    return list(rows)


@router.post("/thread", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_message(payload: MessageIn, user: CurrentUser, db: DbSession) -> MessageOut:
    thread = await _thread_for(db, user)

    message = Message(
        thread_id=thread.id, sender_id=user.id, body=(payload.body or "").strip()
    )
    db.add(message)
    await db.flush()

    attachments = await claim_attachments(
        db,
        attachment_ids=payload.attachment_ids,
        owner_id=user.id,
        message_id=message.id,
    )
    thread.last_message_at = datetime.now(UTC)
    await db.flush()

    message.attachments = attachments
    return serialise_message(message, user.id)