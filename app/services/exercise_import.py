"""Syncing the shipped exercise catalogue into the database.

Two operations live here.

`sync_catalog` writes `app.data.exercise_library.CATALOG` into `exercises`,
idempotently — it inserts what is missing and backfills columns that are still
empty, but it never overwrites something the coach has edited by hand. That
distinction is the whole point: running the sync after a deploy must be safe,
and a coach who replaced a demonstration link with their own recording must not
find it reverted the next time someone redeploys.

`verify_video_links` HEAD-checks every link in the library and reports the dead
ones. The catalogue's demonstration URLs are derived from a slug pattern rather
than scraped — the source site blocks crawlers — so a handful will not resolve.
This is how the coach finds those in one pass instead of one client complaint at
a time.
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

# Politeness and self-protection. Twenty parallel HEAD requests finishes ~200
# links in a few seconds; two hundred parallel would look like an attack from
# the far end and would exhaust the worker's socket budget from this one.
_LINK_CHECK_CONCURRENCY = 20
_LINK_CHECK_TIMEOUT = 8.0


def derive_video_url(name: str) -> str:
    """The demonstration guide for a movement, built from its name.

    The source site's URLs are `/exercises/{slug}`, verified against known
    pages. Deriving rather than storing a hand-written URL per row means a new
    movement added to the catalogue gets a link for free — and means a wrong
    one is a slug fix, not a data migration.
    """
    return f"{VIDEO_BASE}/{slugify(name)}"


@dataclass
class SyncReport:
    created: int = 0
    backfilled: int = 0
    unchanged: int = 0
    skipped_names: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.backfilled + self.unchanged


async def sync_catalog(db: AsyncSession, *, overwrite_videos: bool = False) -> SyncReport:
    """Insert missing movements and fill in blank columns on existing ones.

    `overwrite_videos=False` is the default and the safe one: an exercise that
    already has a `video_url` keeps it, whatever the catalogue says. Pass True
    only when deliberately repointing the whole library — after the source URL
    pattern changes, for instance — and understand that it discards the coach's
    own links.

    The caller commits. This function only flushes, because a partially applied
    catalogue is worse than none: `flush()` without a later `commit()` looks
    like a success in the logs and then rolls back on session close.
    """
    report = SyncReport()

    existing = {
        row.slug: row for row in (await db.execute(select(Exercise))).scalars().all()
    }

    for (
        name,
        group,
        target,
        secondary,
        equipment,
        mechanics,
        force,
        level,
        popularity,
        cue,
    ) in CATALOG:
        slug = slugify(name)
        video_url = derive_video_url(name)
        current = existing.get(slug)

        if current is None:
            db.add(
                Exercise(
                    slug=slug,
                    name=name,
                    muscle_group=group,
                    target_muscle=target,
                    secondary_muscles=list(secondary),
                    equipment=equipment,
                    mechanics=mechanics,
                    force_type=force,
                    min_level=level,
                    popularity=popularity,
                    coaching_cue=cue,
                    video_url=video_url,
                    source_url=video_url,
                    is_active=True,
                )
            )
            report.created += 1
            continue

        # Backfill only. Anything already set was either seeded correctly or
        # edited on purpose, and both deserve to survive a resync.
        changed = False

        if current.muscle_group != group and not current.created_by_id:
            # A movement the coach authored keeps its own classification; one
            # that came from this catalogue gets re-filed if the taxonomy moved.
            current.muscle_group = group
            changed = True
        if not current.coaching_cue:
            current.coaching_cue = cue
            changed = True
        if current.mechanics is None:
            current.mechanics = mechanics
            changed = True
        if current.force_type is None:
            current.force_type = force
            changed = True
        if not current.secondary_muscles:
            current.secondary_muscles = list(secondary)
            changed = True
        if not current.source_url:
            current.source_url = video_url
            changed = True
        if not current.popularity:
            current.popularity = popularity
            changed = True
        if not current.video_url or overwrite_videos:
            current.video_url = video_url
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
                return LinkResult(
                    exercise_id=str(exercise.id),
                    name=exercise.name,
                    url=url,
                    status=response.status_code,
                    ok=response.status_code < 400,
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
        headers={"User-Agent": "CoachAuto-LinkCheck/1.0"},
    ) as client:
        results = await asyncio.gather(*(check(client, row) for row in rows))

    broken = [result for result in results if not result.ok]
    log.info("exercise.links_verified", checked=len(results), broken=len(broken))
    return list(results)