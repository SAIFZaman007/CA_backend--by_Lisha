"""
Find — and optionally repair — database rows whose media file is gone.

Every deploy before the move to Cloudinary threw away the container's upload
volume, so production is left with rows whose `*_key` points at nothing. This
module walks every media column once and sorts each key into:

* `remote`   — already on Cloudinary; nothing to do
* `local`    — the file still exists on this container's disk
* `missing`  — the file is gone; the row renders as a broken image

`--migrate` uploads every `local` file to Cloudinary and rewrites its key, so
it survives the next deploy. `--prune` repairs every `missing` row so nothing
on screen is broken any more:

    programs.image_key            -> NULL (the plan shows no artwork)
    video_tutorials.file_key      -> NULL, and the tutorial is unpublished when
                                     it has no hosted link either
    video_tutorials.thumbnail_key -> NULL (falls back to the provider thumbnail)
    gallery_images                -> row deleted (it is nothing but the image)
    progress_photos               -> row deleted
    message_attachments           -> row deleted (the message text is kept)

Neither flag runs by default: a plain run only reports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.catalog import Program
from app.models.engagement import MessageAttachment
from app.models.gallery import GalleryImage
from app.models.media import VideoTutorial
from app.models.tracking import ProgressPhoto
from app.services import storage

log = get_logger("media.audit")


@dataclass
class ColumnReport:
    label: str
    remote: int = 0
    local: int = 0
    missing: int = 0
    migrated: int = 0
    pruned: int = 0


@dataclass
class AuditReport:
    columns: list[ColumnReport] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return sum(column.missing for column in self.columns)

    @property
    def local(self) -> int:
        return sum(column.local for column in self.columns)


def _classify(key: str | None) -> str | None:
    if not key:
        return None
    if storage.is_remote(key):
        return "remote"
    return "local" if storage.exists(key) else "missing"


async def _walk(
    db: AsyncSession,
    report: ColumnReport,
    rows: list,
    get_key: Callable,
    set_key: Callable,
    on_missing: Callable,
    *,
    migrate: bool,
    prune: bool,
) -> None:
    for row in rows:
        state = _classify(get_key(row))
        if state is None:
            continue
        setattr(report, state, getattr(report, state) + 1)

        if state == "local" and migrate:
            new_key = storage.migrate_local_key(get_key(row))
            if new_key:
                set_key(row, new_key)
                report.migrated += 1
        elif state == "missing" and prune:
            await on_missing(row)
            report.pruned += 1
    await db.flush()


async def audit_media(
    db: AsyncSession, *, migrate: bool = False, prune: bool = False
) -> AuditReport:
    """Walk every media column. The caller commits."""
    if migrate and not storage.is_cloud_enabled():
        raise RuntimeError("--migrate needs Cloudinary configured (CLOUDINARY_URL).")

    report = AuditReport()

    async def delete_row(row) -> None:
        await db.execute(delete(type(row)).where(type(row).id == row.id))

    # --- Programme artwork ---------------------------------------------------
    programs = ColumnReport("programs.image_key")
    report.columns.append(programs)

    async def clear_program(row: Program) -> None:
        row.image_key = None

    await _walk(
        db,
        programs,
        list((await db.execute(select(Program))).scalars().all()),
        lambda row: row.image_key,
        lambda row, key: setattr(row, "image_key", key),
        clear_program,
        migrate=migrate,
        prune=prune,
    )

    # --- Tutorials (two columns) --------------------------------------------
    tutorials = list((await db.execute(select(VideoTutorial))).scalars().all())

    videos = ColumnReport("video_tutorials.file_key")
    report.columns.append(videos)

    async def clear_video(row: VideoTutorial) -> None:
        row.file_key = None
        row.file_size_bytes = None
        if not row.video_url:
            row.is_published = False

    await _walk(
        db,
        videos,
        tutorials,
        lambda row: row.file_key,
        lambda row, key: setattr(row, "file_key", key),
        clear_video,
        migrate=migrate,
        prune=prune,
    )

    posters = ColumnReport("video_tutorials.thumbnail_key")
    report.columns.append(posters)

    async def clear_poster(row: VideoTutorial) -> None:
        row.thumbnail_key = None

    await _walk(
        db,
        posters,
        tutorials,
        lambda row: row.thumbnail_key,
        lambda row, key: setattr(row, "thumbnail_key", key),
        clear_poster,
        migrate=migrate,
        prune=prune,
    )

    # --- Rows that are nothing without their file ----------------------------
    for label, model, attr in (
        ("gallery_images.image_key", GalleryImage, "image_key"),
        ("progress_photos.file_key", ProgressPhoto, "file_key"),
        ("message_attachments.file_key", MessageAttachment, "file_key"),
    ):
        column = ColumnReport(label)
        report.columns.append(column)
        await _walk(
            db,
            column,
            list((await db.execute(select(model))).scalars().all()),
            lambda row, attr=attr: getattr(row, attr),
            lambda row, key, attr=attr: setattr(row, attr, key),
            delete_row,
            migrate=migrate,
            prune=prune,
        )

    log.info(
        "media.audited",
        local=report.local,
        missing=report.missing,
        migrate=migrate,
        prune=prune,
    )
    return report