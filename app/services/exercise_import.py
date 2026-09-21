"""
Syncing the shipped exercise catalogue into the database.

`sync_catalog` writes `app.data.exercise_library.CATALOG` into `exercises`,
idempotently — it inserts what is missing and backfills columns that are still
empty, and it never overwrites a link or cue the coach has edited by hand.

How "edited by hand" is told apart from "written by the catalogue"
------------------------------------------------------------------
Every catalogue row stores the link it was given in *both* `video_url` and
`source_url`. A coach replacing the demonstration changes `video_url` only.
So, for an existing row:

* `video_url` empty                          -> fill it
* `video_url == source_url`                  -> the catalogue wrote it; repoint
* `video_url == <old derived pattern>`       -> the pre-2026-09 guess; repair it
* anything else                              -> the coach's own link; keep it

That rule is what lets a routine deploy repair every broken derived link in an
existing database without an explicit `overwrite_videos` run, while still
never discarding a recording the coach pasted in.

`verify_video_links` HEAD-checks every link and reports the dead ones.
"""

import asyncio
from dataclasses import dataclass, field

import httpx
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.data.exercise_library import CATALOG, VIDEO_BASE
from app.models.catalog import Exercise

log = get_logger("exercise.import")

_LINK_CHECK_CONCURRENCY = 20
_LINK_CHECK_TIMEOUT = 8.0

_BOT_PROTECTED_STATUSES = {401, 403, 429}


def _old_derived_url(name: str) -> str:
    """The link the previous catalogue guessed for a movement. Used only to
    recognise — and repair — rows written by that version."""
    return f"{VIDEO_BASE}/{slugify(name)}"


@dataclass
class SyncReport:
    created: int = 0
    backfilled: int = 0
    unchanged: int = 0
    repaired_links: int = 0
    skipped_names: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.backfilled + self.unchanged


async def sync_catalog(db: AsyncSession, *, overwrite_videos: bool = False) -> SyncReport:
    """
    Insert missing movements, backfill blank columns, repair catalogue links.

    `overwrite_videos=True` repoints *every* row at the catalogue link,
    including ones the coach replaced by hand. Use only on purpose.

    The caller commits. This function only flushes.
    """
    report = SyncReport()

    existing = {
        row.slug: row for row in (await db.execute(select(Exercise))).scalars().all()
    }

    for movement in CATALOG:
        slug = slugify(movement.name)
        video_url = movement.video_url
        current = existing.get(slug)

        if current is None:
            exercise = Exercise(
                slug=slug,
                name=movement.name,
                muscle_group=movement.group,
                target_muscle=movement.target,
                secondary_muscles=list(movement.secondary),
                equipment=movement.equipment,
                mechanics=movement.mechanics,
                force_type=movement.force,
                min_level=movement.level,
                popularity=movement.popularity,
                coaching_cue=movement.cue,
                video_url=video_url,
                source_url=video_url,
                is_active=True,
            )
            db.add(exercise)
            existing[slug] = exercise
            report.created += 1
            continue

        changed = False

        if current.muscle_group != movement.group and not current.created_by_id:
            current.muscle_group = movement.group
            changed = True
        if not current.coaching_cue and movement.cue:
            current.coaching_cue = movement.cue
            changed = True
        if current.mechanics is None:
            current.mechanics = movement.mechanics
            changed = True
        if current.force_type is None:
            current.force_type = movement.force
            changed = True
        if not current.secondary_muscles and movement.secondary:
            current.secondary_muscles = list(movement.secondary)
            changed = True
        if not current.popularity:
            current.popularity = movement.popularity
            changed = True

        catalogue_owned = (
            not current.video_url
            or current.video_url == current.source_url
            or current.video_url == _old_derived_url(current.name)
        )
        if (catalogue_owned or overwrite_videos) and current.video_url != video_url:
            current.video_url = video_url
            report.repaired_links += 1
            changed = True
        if current.source_url != video_url:
            current.source_url = video_url
            changed = True

        if changed:
            report.backfilled += 1
        else:
            report.unchanged += 1

    await db.flush()
    log.info(
        "exercise.catalog_synced",
        created=report.created,
        backfilled=report.backfilled,
        repaired_links=report.repaired_links,
        unchanged=report.unchanged,
    )
    return report


@dataclass
class LinkResult:
    exercise_id: str
    name: str
    url: str
    status: int | None
    ok: bool
    error: str | None = None


async def verify_video_links(db: AsyncSession, *, limit: int | None = None) -> list[LinkResult]:
    """HEAD every demonstration link and report which ones do not resolve.

    Returns results for *all* checked links, not just the failures — the
    dashboard shows a pass count alongside the problems, and "0 broken" is
    only reassuring if you know how many were tested.
    """
    stmt = select(Exercise).where(
        Exercise.is_active.is_(True), Exercise.video_url.is_not(None)
    ).order_by(Exercise.name)
    if limit:
        stmt = stmt.limit(limit)

    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return []

    semaphore = asyncio.Semaphore(_LINK_CHECK_CONCURRENCY)

    async def check(client: httpx.AsyncClient, exercise: Exercise) -> LinkResult:
        async with semaphore:
            url = exercise.video_url or ""
            try:
                # Some hosts refuse HEAD outright; fall back to a ranged GET
                # that asks for the first byte rather than downloading a page.
                response = await client.head(url, follow_redirects=True)
                if response.status_code in (403, 405, 501):
                    response = await client.get(
                        url, follow_redirects=True, headers={"Range": "bytes=0-0"}
                    )
                blocked = response.status_code in _BOT_PROTECTED_STATUSES
                return LinkResult(
                    exercise_id=str(exercise.id),
                    name=exercise.name,
                    url=url,
                    status=response.status_code,
                    ok=response.status_code < 400 or blocked,
                    error="bot-protected (not verifiable from the server)" if blocked else None,
                )
            except httpx.HTTPError as exc:
                return LinkResult(
                    exercise_id=str(exercise.id),
                    name=exercise.name,
                    url=url,
                    status=None,
                    ok=False,
                    error=type(exc).__name__,
                )

    async with httpx.AsyncClient(
        timeout=_LINK_CHECK_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CoachAuto-LinkCheck/1.1)"},
    ) as client:
        results = await asyncio.gather(*(check(client, row) for row in rows))

    broken = [result for result in results if not result.ok]
    log.info("exercise.links_verified", checked=len(results), broken=len(broken))
    return list(results)