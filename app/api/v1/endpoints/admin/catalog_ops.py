"""Exercise catalogue operations for the coach dashboard.

Two jobs the exercise CRUD routes cannot do, because both are bulk operations
over the whole library rather than edits to one row.

`sync` writes the catalogue shipped in `app.data.exercise_library` into the
database. Additive and idempotent: it inserts what is missing and backfills
blanks, and never overwrites a link or a cue the coach has edited by hand.

`verify-links` HEAD-checks every demonstration URL and reports the dead ones.
The catalogue's links are derived from a slug pattern rather than scraped — the
source site blocks crawlers — so a handful will not resolve. This is how the
coach finds all of them in one pass rather than one client complaint at a time.
"""

from fastapi import APIRouter, Query

from app.core.deps import CurrentCoach, DbSession
from app.core.logging import get_logger
from app.data.exercise_library import catalog_size
from app.schemas.catalog import (
    CatalogSyncOut,
    LinkCheckOut,
    LinkCheckRow,
)
from app.services.exercise_import import sync_catalog, verify_video_links

router = APIRouter()
log = get_logger("admin.catalog_ops")


@router.post("/exercises/sync", response_model=CatalogSyncOut)
async def sync_exercise_catalog(
    coach: CurrentCoach,
    db: DbSession,
    overwrite_videos: bool = Query(
        False,
        description=(
            "Repoint every demonstration link at the shipped catalogue's URL, "
            "discarding any the coach has replaced by hand. Off by default."
        ),
    ),
) -> CatalogSyncOut:
    """Import or refresh the shipped exercise catalogue.

    Safe to run after every deploy. The default leaves edited rows alone, so
    the only way this loses work is by explicitly asking it to.
    """
    report = await sync_catalog(db, overwrite_videos=overwrite_videos)
    log.info(
        "admin.catalog_synced",
        by=str(coach.id),
        created=report.created,
        backfilled=report.backfilled,
    )
    return CatalogSyncOut(
        created=report.created,
        backfilled=report.backfilled,
        unchanged=report.unchanged,
        catalog_size=catalog_size(),
    )


@router.post("/exercises/verify-links", response_model=LinkCheckOut)
async def verify_exercise_links(
    coach: CurrentCoach,
    db: DbSession,
    limit: int | None = Query(
        None,
        ge=1,
        le=1000,
        description="Check only the first N movements. Useful for a quick spot check.",
    ),
) -> LinkCheckOut:
    """HEAD every demonstration link and report which ones do not resolve.

    Returns the failures in full and only a count of the passes. A coach acting
    on this needs the broken list; two hundred rows of "200 OK" is noise that
    buries it.
    """
    results = await verify_video_links(db, limit=limit)
    broken = [row for row in results if not row.ok]

    return LinkCheckOut(
        checked=len(results),
        ok=len(results) - len(broken),
        broken=[
            LinkCheckRow(
                exercise_id=row.exercise_id,
                name=row.name,
                url=row.url,
                status=row.status,
                error=row.error,
            )
            for row in broken
        ],
    )