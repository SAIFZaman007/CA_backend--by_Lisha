"""
Who is on which pricing plan — the one answer every "N clients" badge reads.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Subscription
from app.models.catalog import Program
from app.models.enums import ENTITLING_STATUSES, TrainingLevel, UserRole
from app.models.user import ClientProfile, User


def _plan_order(program: Program) -> tuple:
    """
    Pricing-page order, with listed plans ahead of archived ones.

    Used to pick the single tier that owns a level when more than one plan
    shares it (e.g. an archived "Level 1" kept for billing history next to the
    current one). Deterministic, so a badge never jumps between cards.
    """
    return (
        not program.is_active,
        program.sort_order,
        program.price_cents,
        program.created_at,
        str(program.id),
    )


def level_owners(programs: Iterable[Program]) -> dict[TrainingLevel, uuid.UUID]:
    """The one plan that represents each training level on the roster."""
    owners: dict[TrainingLevel, uuid.UUID] = {}
    for program in sorted(programs, key=_plan_order):
        owners.setdefault(program.level, program.id)
    return owners


async def program_client_counts(
    db: AsyncSession, programs: Iterable[Program] | None = None
) -> dict[uuid.UUID, int]:
    """
    Active clients per plan id. Plans nobody is on are simply absent.

    `programs` is the plan list the caller already loaded (the admin list
    endpoint has it in hand); it is only needed to resolve which plan owns a
    level. When omitted, every plan is loaded — a handful of rows.
    """
    if programs is None:
        programs = (await db.execute(select(Program))).scalars().all()
    programs = list(programs)
    if not programs:
        return {}

    # Newest entitling subscription per client — the same row
    # `entitlements.active_subscription` resolves, expressed as a set.
    live_subscription = (
        select(Subscription.client_id, Subscription.program_id)
        .where(
            Subscription.status.in_(ENTITLING_STATUSES),
            Subscription.program_id.is_not(None),
        )
        .distinct(Subscription.client_id)
        .order_by(Subscription.client_id, Subscription.created_at.desc())
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                live_subscription.c.program_id,
                ClientProfile.level,
                func.count(User.id),
            )
            .select_from(User)
            .outerjoin(ClientProfile, ClientProfile.user_id == User.id)
            .outerjoin(live_subscription, live_subscription.c.client_id == User.id)
            .where(
                User.role == UserRole.CLIENT,
                User.is_active.is_(True),
                or_(
                    live_subscription.c.program_id.is_not(None),
                    ClientProfile.level.is_not(None),
                ),
            )
            .group_by(live_subscription.c.program_id, ClientProfile.level)
        )
    ).all()

    owners = level_owners(programs)
    counts: dict[uuid.UUID, int] = defaultdict(int)
    for subscribed_program_id, level, total in rows:
        if subscribed_program_id is not None:
            counts[subscribed_program_id] += total
        elif level is not None and level in owners:
            counts[owners[level]] += total
    return dict(counts)


async def program_client_count(db: AsyncSession, program: Program) -> int:
    """Convenience for single-plan responses (create, update, delete guard)."""
    return (await program_client_counts(db)).get(program.id, 0)