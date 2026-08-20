"""The dashboard landing screen: what changed, and who needs the coach today."""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, select

from app.core.deps import CurrentCoach, DbSession
from app.models.catalog import Program
from app.models.engagement import ConsultationBooking, Lead, Message, MessageThread
from app.models.enums import BookingStatus, LeadStatus, UserRole
from app.models.media import VideoTutorial
from app.models.tracking import WeightLog
from app.models.training import WorkoutSession
from app.models.user import User
from app.schemas.admin import (
    AttentionItem,
    LeadOut,
    MetricPoint,
    OverviewCounts,
    OverviewOut,
)

router = APIRouter()

# A client who has not logged a weight in this many days is surfaced.
STALE_CHECKIN_DAYS = 10


def _client_filter(stmt: Select) -> Select:
    return stmt.where(User.role == UserRole.CLIENT)


async def _daily_counts(db: DbSession, stmt: Select, days: int) -> list[MetricPoint]:
    """Turn a (date, count) query into a dense series with zero-filled gaps, so
    the chart never invents a slope across a quiet week."""
    rows = {row[0]: float(row[1]) for row in (await db.execute(stmt)).all()}
    today = date.today()
    return [
        MetricPoint(log_date=day, value=rows.get(day, 0.0))
        for day in (today - timedelta(days=days - 1 - i) for i in range(days))
    ]


@router.get("/overview", response_model=OverviewOut)
async def overview(
    coach: CurrentCoach, db: DbSession, days: int = Query(30, ge=7, le=180)
) -> OverviewOut:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    today = date.today()

    active_clients = (
        await db.execute(
            _client_filter(select(func.count(User.id))).where(User.is_active.is_(True))
        )
    ).scalar_one()
    inactive_clients = (
        await db.execute(
            _client_filter(select(func.count(User.id))).where(User.is_active.is_(False))
        )
    ).scalar_one()
    new_clients = (
        await db.execute(
            _client_filter(select(func.count(User.id))).where(
                User.created_at >= now - timedelta(days=30)
            )
        )
    ).scalar_one()

    new_leads = (
        await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.NEW))
    ).scalar_one()
    pending_bookings = (
        await db.execute(
            select(func.count(ConsultationBooking.id)).where(
                ConsultationBooking.status == BookingStatus.REQUESTED
            )
        )
    ).scalar_one()

    # Unread = sent by the client, not yet read by the coach.
    unread_messages = (
        await db.execute(
            select(func.count(Message.id))
            .join(MessageThread, MessageThread.id == Message.thread_id)
            .where(Message.read_at.is_(None), Message.sender_id == MessageThread.client_id)
        )
    ).scalar_one()

    published_tutorials = (
        await db.execute(
            select(func.count(VideoTutorial.id)).where(VideoTutorial.is_published.is_(True))
        )
    ).scalar_one()
    active_programs = (
        await db.execute(select(func.count(Program.id)).where(Program.is_active.is_(True)))
    ).scalar_one()

    signups = await _daily_counts(
        db,
        _client_filter(
            select(func.date(User.created_at).label("d"), func.count(User.id))
        )
        .where(User.created_at >= since)
        .group_by("d"),
        days,
    )
    sessions = await _daily_counts(
        db,
        select(WorkoutSession.session_date, func.count(WorkoutSession.id))
        .where(WorkoutSession.session_date >= since.date())
        .group_by(WorkoutSession.session_date),
        days,
    )

    # --- Who needs a nudge ----------------------------------------------------
    attention: list[AttentionItem] = []

    last_weight = (
        select(
            WeightLog.client_id.label("client_id"),
            func.max(WeightLog.log_date).label("last_log"),
        )
        .group_by(WeightLog.client_id)
        .subquery()
    )
    rows = (
        await db.execute(
            _client_filter(
                select(User.id, User.full_name, User.display_name, last_weight.c.last_log)
            )
            .outerjoin(last_weight, last_weight.c.client_id == User.id)
            .where(User.is_active.is_(True))
            .order_by(last_weight.c.last_log.asc().nullsfirst())
            .limit(12)
        )
    ).all()

    for user_id, full_name, display_name, last_log in rows:
        name = display_name or full_name
        if last_log is None:
            attention.append(
                AttentionItem(
                    client_id=user_id, client_name=name, reason="No weight logged yet", days=None
                )
            )
            continue
        gap = (today - last_log).days
        if gap >= STALE_CHECKIN_DAYS:
            attention.append(
                AttentionItem(
                    client_id=user_id,
                    client_name=name,
                    reason=f"No check-in for {gap} days",
                    days=gap,
                )
            )

    recent_leads = list(
        (
            await db.execute(select(Lead).order_by(Lead.created_at.desc()).limit(5))
        )
        .scalars()
        .all()
    )

    return OverviewOut(
        counts=OverviewCounts(
            active_clients=active_clients,
            inactive_clients=inactive_clients,
            new_clients_30d=new_clients,
            new_leads=new_leads,
            pending_bookings=pending_bookings,
            unread_messages=unread_messages,
            published_tutorials=published_tutorials,
            active_programs=active_programs,
        ),
        signups=signups,
        sessions=sessions,
        needs_attention=attention[:8],
        recent_leads=[LeadOut.model_validate(lead) for lead in recent_leads],
    )