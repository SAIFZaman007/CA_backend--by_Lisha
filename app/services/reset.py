"""
The destructive counterpart to `app.services.seed`.

"""

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for the side effect of registering every table on `Base.metadata`.
# Without it a full reset silently truncates only the handful of tables whose
# modules happen to have been imported already.
import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base
from app.core.logging import get_logger
from app.models.engagement import (
    ConsultationBooking,
    Lead,
    Message,
    MessageAttachment,
    MessageThread,
)
from app.models.catalog import Testimonial
from app.models.tracking import ProgressPhoto
from app.models.user import User
from app.services.seed import BOOKINGS, CLIENTS, LEADS, TESTIMONIALS

log = get_logger("reset")

# Never truncated. Alembic's bookmark is metadata about the schema, not
# application data — clearing it would leave `upgrade head` believing it had
# never run and re-applying migrations against tables that already exist.
PRESERVED_TABLES = frozenset({"alembic_version"})


@dataclass(slots=True)
class ResetReport:
    """What a reset actually did. Returned rather than printed, so the CLI
    owns the wording and tests can assert on the numbers."""

    scope: str
    tables_cleared: int = 0
    accounts_deleted: int = 0
    leads_deleted: int = 0
    bookings_deleted: int = 0
    testimonials_deleted: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def megabytes_freed(self) -> float:
        return round(self.bytes_freed / (1024 * 1024), 2)


# --- Media ---------------------------------------------------------------------


def _upload_root() -> Path:
    return Path(settings.UPLOAD_DIR)


def _safe_path(key: str) -> Path | None:
    """
    Resolve a stored key inside the upload root, or None if it escapes.

    `storage.resolve_path` would be the obvious thing to reuse, but it raises
    `HTTPException` and requires the file to exist — both wrong here. A reset
    walking a hundred keys should skip a missing file quietly and keep going,
    not raise an HTTP error out of a CLI command.
    """
    if not key:
        return None
    root = _upload_root().resolve()
    candidate = (root / key).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _delete_keys(keys: Iterable[str]) -> tuple[int, int]:
    """Unlink each key. Returns (files deleted, bytes freed)."""
    deleted = 0
    freed = 0
    touched_dirs: set[Path] = set()

    for key in keys:
        path = _safe_path(key)
        if path is None or not path.is_file():
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
            deleted += 1
            touched_dirs.add(path.parent)
        except OSError as exc:  # a permissions problem should not abort the reset
            log.warning("reset.file_unlink_failed", key=key, error=str(exc))

    _prune_empty(touched_dirs)
    return deleted, freed


def _prune_empty(directories: set[Path]) -> None:
    """
    Remove the date/client folders a delete has just emptied.

    Uploads are stored under `<client-id>/<date>/`, so removing one client's
    photos leaves a tree of empty directories behind. Walks upward until it
    reaches a directory that still holds something, or the upload root.
    """
    root = _upload_root().resolve()
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        current = directory
        while current != root and current.is_relative_to(root):
            try:
                if any(current.iterdir()):
                    break
                current.rmdir()
            except OSError:
                break
            current = current.parent


def _purge_upload_root() -> tuple[int, int]:
    """
    Empty the upload volume, keeping the root directory itself.

    Only ever called for a full reset, where every row that could reference a
    file has just been truncated. Removing the root as well would break the
    next write on deployments that bind-mount it, so its children go and it
    stays.
    """
    root = _upload_root()
    if not root.is_dir():
        return 0, 0

    deleted = 0
    freed = 0
    for entry in root.iterdir():
        if entry.is_file():
            freed += entry.stat().st_size
            entry.unlink(missing_ok=True)
            deleted += 1
            continue
        for path in entry.rglob("*"):
            if path.is_file():
                freed += path.stat().st_size
                deleted += 1
        shutil.rmtree(entry, ignore_errors=True)

    return deleted, freed


# --- Demo scope ----------------------------------------------------------------

DEMO_CLIENT_EMAILS: tuple[str, ...] = tuple(spec["email"].lower() for spec in CLIENTS)
DEMO_LEAD_EMAILS: tuple[str, ...] = tuple(spec["email"].lower() for spec in LEADS)
DEMO_BOOKING_EMAILS: tuple[str, ...] = tuple(spec["email"].lower() for spec in BOOKINGS)
DEMO_TESTIMONIAL_NAMES: tuple[str, ...] = tuple(row[0] for row in TESTIMONIALS)


async def _demo_media_keys(db: AsyncSession, client_ids: Sequence) -> list[str]:
    """
    Every file key reachable from these accounts.

    Read before anything is deleted, because a foreign key with ON DELETE
    CASCADE removes the row and the key written on it in the same breath —
    there is no second chance to ask where the bytes were.

    Two sources. Check-in photos belong to the client directly. Attachments
    belong to a message in the client's thread, which includes images the
    *coach* sent into that conversation: those are part of the demo data and
    go with it. An attachment the coach uploaded and never sent is not — it is
    sitting in her composer right now — so the join is through `messages`
    rather than through `uploaded_by_id`.
    """
    if not client_ids:
        return []

    photo_keys = (
        (
            await db.execute(
                select(ProgressPhoto.file_key).where(ProgressPhoto.client_id.in_(client_ids))
            )
        )
        .scalars()
        .all()
    )

    attachment_keys = (
        (
            await db.execute(
                select(MessageAttachment.file_key)
                .join(Message, Message.id == MessageAttachment.message_id)
                .join(MessageThread, MessageThread.id == Message.thread_id)
                .where(MessageThread.client_id.in_(client_ids))
            )
        )
        .scalars()
        .all()
    )

    return [*photo_keys, *attachment_keys]


async def reset_demo(db: AsyncSession, *, drop_media: bool = True) -> ResetReport:
    """
    Remove the seeded demo data and nothing else.

    The account delete does most of the work on its own. Every table hanging
    off a client — profile, plans, meal plans, logs, measurements, photos,
    threads, messages, attachments, subscriptions, refresh sessions — is wired
    with `ON DELETE CASCADE`, so one statement per scope is both the fastest
    way to do this and the only one that cannot leave an orphan behind by
    forgetting a table somebody added last month.
    """
    report = ResetReport(scope="demo")

    client_ids = (
        (
            await db.execute(
                select(User.id).where(User.email.in_(DEMO_CLIENT_EMAILS))
            )
        )
        .scalars()
        .all()
    )

    keys = await _demo_media_keys(db, client_ids) if drop_media else []

    async def _purge(statement) -> int:
        result = await db.execute(
            statement.execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    if client_ids:
        report.accounts_deleted = await _purge(delete(User).where(User.id.in_(client_ids)))

    report.leads_deleted = await _purge(delete(Lead).where(Lead.email.in_(DEMO_LEAD_EMAILS)))

    report.bookings_deleted = await _purge(
        delete(ConsultationBooking).where(ConsultationBooking.email.in_(DEMO_BOOKING_EMAILS))
    )

    report.testimonials_deleted = await _purge(
        delete(Testimonial).where(Testimonial.client_name.in_(DEMO_TESTIMONIAL_NAMES))
    )

    await db.commit()

    if keys:
        report.files_deleted, report.bytes_freed = _delete_keys(keys)

    log.info(
        "reset.demo_complete",
        accounts=report.accounts_deleted,
        leads=report.leads_deleted,
        bookings=report.bookings_deleted,
        files=report.files_deleted,
    )
    return report


# --- Full scope ----------------------------------------------------------------


def application_tables() -> list[str]:
    """
    Every table the application owns, in dependency order.

    Taken from the live metadata rather than a hand-maintained list, so a table
    added in a future migration is included the day its model lands instead of
    the day somebody notices it survived a reset.
    """
    return [
        table.name
        for table in Base.metadata.sorted_tables
        if table.name not in PRESERVED_TABLES
    ]


async def reset_all(db: AsyncSession, *, drop_media: bool = True) -> ResetReport:
    """
    Empty every application table.

    One `TRUNCATE` naming all of them together, which matters for more than
    speed: truncating tables one at a time fails on the first foreign key
    pointing back at a table already emptied, and ordering them by hand is a
    list that goes stale. Naming them in a single statement makes the whole
    thing one atomic operation with no ordering to get wrong.

    `RESTART IDENTITY` resets sequences. Every primary key here is a UUID, so
    it changes nothing today — it is there so that the day someone adds a table
    with a serial column, a reset does not hand out ids that continue from
    where deleted rows left off.
    """
    report = ResetReport(scope="all")
    tables = application_tables()
    if not tables:
        report.warnings.append("No application tables found — has `alembic upgrade head` run?")
        return report

    quoted = ", ".join(f'"{name}"' for name in tables)
    await db.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    await db.commit()
    report.tables_cleared = len(tables)

    if drop_media:
        report.files_deleted, report.bytes_freed = _purge_upload_root()

    log.info("reset.all_complete", tables=report.tables_cleared, files=report.files_deleted)
    return report