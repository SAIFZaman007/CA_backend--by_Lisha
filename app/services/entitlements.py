"""What a client is allowed to do, derived from what they have paid for.

This is the single place that answers "is this person entitled to X?". Every
guard in the API and every locked panel in the portal reads from here, so the
rule lives once instead of being re-implemented per screen and drifting.

The chain is deliberately one-directional:

    Stripe subscription  →  Program (the tier)  →  TrainingLevel  →  features

A client's `profile.level` is a *cache* of the middle step, written when a
subscription starts and cleared when it ends. Nothing reads it to make an
access decision — access is always decided from the live subscription, so a
stale cache can never hand out coaching nobody paid for.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Subscription
from app.models.enums import ENTITLING_STATUSES, TrainingLevel
from app.models.user import ClientProfile, User

# --- The feature matrix -------------------------------------------------------
#
# Higher tiers are supersets of lower ones, so each level lists only what it
# adds. `features_for` walks the ladder and unions them. Adding a tier means
# adding one row here, not hunting through route handlers.

BASE_FEATURES: frozenset[str] = frozenset(
    {
        "dashboard",
        "workouts",
        "exercise_videos",
        "tutorials",
        "meal_plan",
        "progress_tracking",
        "check_in_photos",
        "sleep_cardio",
        "calculators",
        "messaging",
    }
)

LEVEL_ADDITIONS: dict[TrainingLevel, frozenset[str]] = {
    TrainingLevel.LEVEL_1: frozenset(),
    TrainingLevel.LEVEL_2: frozenset({"programme_review", "priority_replies", "macro_phases"}),
    TrainingLevel.LEVEL_3: frozenset(
        {"video_form_review", "same_day_replies", "peak_week_guidance", "advanced_analytics"}
    ),
}

LEVEL_ORDER: list[TrainingLevel] = [
    TrainingLevel.LEVEL_1,
    TrainingLevel.LEVEL_2,
    TrainingLevel.LEVEL_3,
]

# Everything a signed-in client can reach with no subscription at all. Enough to
# manage their account and buy a plan — nothing that constitutes coaching.
UNSUBSCRIBED_FEATURES: frozenset[str] = frozenset({"profile", "billing", "calculators"})


def features_for(level: TrainingLevel | None) -> frozenset[str]:
    """Every feature a tier unlocks, including everything below it."""
    if level is None:
        return UNSUBSCRIBED_FEATURES

    unlocked = set(BASE_FEATURES) | set(UNSUBSCRIBED_FEATURES)
    for rung in LEVEL_ORDER:
        unlocked |= LEVEL_ADDITIONS.get(rung, frozenset())
        if rung is level:
            break
    return frozenset(unlocked)


@dataclass(slots=True)
class Entitlement:
    """The full answer for one client, assembled once per request."""

    level: TrainingLevel | None = None
    features: frozenset[str] = field(default_factory=lambda: UNSUBSCRIBED_FEATURES)
    subscription: Subscription | None = None
    program_name: str | None = None
    program_slug: str | None = None

    @property
    def is_subscribed(self) -> bool:
        return self.level is not None

    def has(self, feature: str) -> bool:
        return feature in self.features

    def to_dict(self) -> dict:
        """The shape the portal reads to decide what to render."""
        subscription = self.subscription
        return {
            "is_subscribed": self.is_subscribed,
            "level": self.level.value if self.level else None,
            "features": sorted(self.features),
            "program": (
                {"name": self.program_name, "slug": self.program_slug}
                if self.program_name
                else None
            ),
            "subscription": (
                {
                    "id": str(subscription.id),
                    "status": subscription.status.value,
                    "price_cents": subscription.price_cents,
                    "currency": subscription.currency,
                    "billing_period": subscription.billing_period,
                    "current_period_end": (
                        subscription.current_period_end.isoformat()
                        if subscription.current_period_end
                        else None
                    ),
                    "cancel_at_period_end": subscription.cancel_at_period_end,
                }
                if subscription
                else None
            ),
        }


async def active_subscription(db: AsyncSession, client_id: uuid.UUID) -> Subscription | None:
    """The live subscription for a client, if any.

    Ordered newest-first so that an upgrade taking effect the same day resolves
    to the tier just bought rather than the one being replaced.
    """
    stmt = (
        select(Subscription)
        .where(
            Subscription.client_id == client_id,
            Subscription.status.in_(ENTITLING_STATUSES),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def entitlement_for(db: AsyncSession, user: User) -> Entitlement:
    """Resolve one user's entitlement.

    Staff are not customers. A coach or admin has no subscription and should
    still be able to open every screen, so they short-circuit to the top tier.
    """
    if user.role.value in ("coach", "admin"):
        return Entitlement(
            level=TrainingLevel.LEVEL_3,
            features=features_for(TrainingLevel.LEVEL_3),
            program_name="Staff access",
        )

    subscription = await active_subscription(db, user.id)
    if subscription is None or subscription.program is None:
        return Entitlement()

    level = subscription.program.level
    return Entitlement(
        level=level,
        features=features_for(level),
        subscription=subscription,
        program_name=subscription.program.name,
        program_slug=subscription.program.slug,
    )


async def sync_profile_level(db: AsyncSession, client_id: uuid.UUID) -> TrainingLevel | None:
    """Write the cached level onto the client's profile after a billing change.

    Called from the webhook handler. The coach's roster and plan builder read
    `profile.level` for display and for picking sensible defaults; keeping it in
    step means those screens do not each need to resolve a subscription.
    """
    subscription = await active_subscription(db, client_id)
    level = subscription.program.level if subscription and subscription.program else None

    profile = (
        await db.execute(select(ClientProfile).where(ClientProfile.user_id == client_id))
    ).scalars().first()

    if profile is None:
        profile = ClientProfile(user_id=client_id, level=level)
        db.add(profile)
    else:
        profile.level = level

    await db.flush()
    return level