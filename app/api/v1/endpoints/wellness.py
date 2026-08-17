"""Sleep and cardio tracking.

The client types their data in regularly; these endpoints store it and roll it
up so both they and their coach can see consistency over time. A fitness watch
can be the source of the numbers, but nothing here depends on one — every field
can be entered by hand.
"""

import uuid
from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.models.enums import CardioType
from app.models.tracking import CardioLog, SleepLog
from app.schemas.tracking import (
    CardioLogIn,
    CardioLogOut,
    SleepLogIn,
    SleepLogOut,
    WellnessSummary,
)
from app.services.calculators import cardio_burn
from app.schemas.tracking import CardioBurnRequest

router = APIRouter(prefix="/wellness", tags=["wellness"])


# --- Sleep ---------------------------------------------------------------------


@router.get("/sleep", response_model=list[SleepLogOut])
async def list_sleep(
    user: CurrentUser, db: DbSession, days: int = Query(30, ge=1, le=365)
) -> list[SleepLog]:
    since = date.today() - timedelta(days=days)
    stmt = (
        select(SleepLog)
        .where(SleepLog.client_id == user.id, SleepLog.log_date >= since)
        .order_by(SleepLog.log_date.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


@router.put("/sleep", response_model=SleepLogOut)
async def log_sleep(payload: SleepLogIn, user: CurrentUser, db: DbSession) -> SleepLog:
    """One night per date. Logging the same date again corrects the entry."""
    target = payload.log_date or date.today()
    if target > date.today():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="You cannot log a night that has not happened yet."
        )

    data = payload.model_dump(exclude={"log_date"})
    existing = (
        await db.execute(
            select(SleepLog).where(SleepLog.client_id == user.id, SleepLog.log_date == target)
        )
    ).scalar_one_or_none()

    if existing:
        for field, value in data.items():
            setattr(existing, field, value)
        await db.flush()
        return existing

    row = SleepLog(client_id=user.id, log_date=target, **data)
    db.add(row)
    await db.flush()
    return row


@router.delete("/sleep/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sleep(log_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    row = (
        await db.execute(
            select(SleepLog).where(SleepLog.id == log_id, SleepLog.client_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That sleep entry was not found.")
    await db.delete(row)


# --- Cardio --------------------------------------------------------------------


@router.get("/cardio", response_model=list[CardioLogOut])
async def list_cardio(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(30, ge=1, le=365),
    activity_type: CardioType | None = None,
) -> list[CardioLog]:
    since = date.today() - timedelta(days=days)
    stmt = select(CardioLog).where(
        CardioLog.client_id == user.id, CardioLog.log_date >= since
    )
    if activity_type:
        stmt = stmt.where(CardioLog.activity_type == activity_type)
    stmt = stmt.order_by(CardioLog.log_date.desc(), CardioLog.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


@router.post("/cardio", response_model=CardioLogOut, status_code=status.HTTP_201_CREATED)
async def log_cardio(payload: CardioLogIn, user: CurrentUser, db: DbSession) -> CardioLog:
    """Add a cardio bout. More than one per day is normal, so these are not
    deduplicated by date the way sleep and weight are.

    If the client does not have a watch to read calories off, we estimate the
    burn from their bodyweight and the activity's energy cost.
    """
    target = payload.log_date or date.today()
    if target > date.today():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="You cannot log a session in the future."
        )

    data = payload.model_dump(exclude={"log_date"})

    if data.get("calories_burned") is None:
        weight = user.profile.current_weight_kg if user.profile else None
        if weight:
            data["calories_burned"] = cardio_burn(
                CardioBurnRequest(
                    activity_type=payload.activity_type,
                    duration_minutes=payload.duration_minutes,
                    weight_kg=float(weight),
                    intensity=payload.intensity,
                )
            ).calories_burned

    row = CardioLog(client_id=user.id, log_date=target, **data)
    db.add(row)
    await db.flush()
    return row


@router.patch("/cardio/{log_id}", response_model=CardioLogOut)
async def update_cardio(
    log_id: uuid.UUID, payload: CardioLogIn, user: CurrentUser, db: DbSession
) -> CardioLog:
    row = (
        await db.execute(
            select(CardioLog).where(CardioLog.id == log_id, CardioLog.client_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That cardio entry was not found.")

    for field, value in payload.model_dump(exclude_unset=True, exclude={"log_date"}).items():
        setattr(row, field, value)
    if payload.log_date:
        row.log_date = payload.log_date
    await db.flush()
    return row


@router.delete("/cardio/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cardio(log_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    row = (
        await db.execute(
            select(CardioLog).where(CardioLog.id == log_id, CardioLog.client_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That cardio entry was not found.")
    await db.delete(row)


# --- Roll-ups ------------------------------------------------------------------


@router.get("/summary", response_model=WellnessSummary)
async def wellness_summary(
    user: CurrentUser, db: DbSession, days: int = Query(7, ge=1, le=90)
) -> WellnessSummary:
    since = date.today() - timedelta(days=days - 1)
    profile = user.profile

    sleep_stats = (
        await db.execute(
            select(
                func.avg(SleepLog.hours_slept),
                func.count(SleepLog.id),
                func.avg(SleepLog.quality),
            ).where(SleepLog.client_id == user.id, SleepLog.log_date >= since)
        )
    ).one()

    cardio_rows = (
        await db.execute(
            select(CardioLog).where(
                CardioLog.client_id == user.id, CardioLog.log_date >= since
            )
        )
    ).scalars().all()

    activity_counts = Counter(row.activity_type for row in cardio_rows)

    return WellnessSummary(
        avg_sleep_hours=round(float(sleep_stats[0]), 1) if sleep_stats[0] else 0.0,
        sleep_target_hours=float(profile.sleep_target_hours) if profile else 8.0,
        nights_logged=sleep_stats[1] or 0,
        avg_sleep_quality=round(float(sleep_stats[2]), 1) if sleep_stats[2] else None,
        cardio_minutes=sum(row.duration_minutes for row in cardio_rows),
        cardio_target_minutes=profile.weekly_cardio_target_min if profile else 150,
        cardio_sessions=len(cardio_rows),
        cardio_calories=sum(row.calories_burned or 0 for row in cardio_rows),
        top_activity=activity_counts.most_common(1)[0][0] if activity_counts else None,
        days_in_window=days,
    )


@router.get("/trends")
async def wellness_trends(
    user: CurrentUser, db: DbSession, days: int = Query(14, ge=7, le=90)
) -> dict:
    """Day-by-day series for the sleep and cardio charts."""
    since = date.today() - timedelta(days=days - 1)

    sleep_rows = (
        await db.execute(
            select(SleepLog.log_date, SleepLog.hours_slept, SleepLog.quality)
            .where(SleepLog.client_id == user.id, SleepLog.log_date >= since)
            .order_by(SleepLog.log_date)
        )
    ).all()

    cardio_rows = (
        await db.execute(
            select(
                CardioLog.log_date,
                func.sum(CardioLog.duration_minutes),
                func.sum(CardioLog.calories_burned),
            )
            .where(CardioLog.client_id == user.id, CardioLog.log_date >= since)
            .group_by(CardioLog.log_date)
            .order_by(CardioLog.log_date)
        )
    ).all()

    by_type = (
        await db.execute(
            select(
                CardioLog.activity_type,
                func.sum(CardioLog.duration_minutes),
                func.count(CardioLog.id),
            )
            .where(CardioLog.client_id == user.id, CardioLog.log_date >= since)
            .group_by(CardioLog.activity_type)
            .order_by(func.sum(CardioLog.duration_minutes).desc())
        )
    ).all()

    return {
        "sleep": [
            {
                "date": row[0].isoformat(),
                "hours": float(row[1]),
                "quality": row[2],
            }
            for row in sleep_rows
        ],
        "cardio": [
            {
                "date": row[0].isoformat(),
                "minutes": int(row[1] or 0),
                "calories": int(row[2] or 0),
            }
            for row in cardio_rows
        ],
        "cardio_by_type": [
            {"activity_type": row[0].value, "minutes": int(row[1] or 0), "sessions": row[2]}
            for row in by_type
        ],
    }


@router.get("/activity-types")
async def activity_types() -> list[dict[str, str]]:
    """Labels for the cardio picker, kept server-side so they stay consistent."""
    labels = {
        CardioType.WALKING: "Walking",
        CardioType.RUNNING: "Running",
        CardioType.CYCLING: "Cycling",
        CardioType.ROWING: "Rowing",
        CardioType.ELLIPTICAL: "Elliptical",
        CardioType.STAIR_CLIMBER: "Stair climber",
        CardioType.SWIMMING: "Swimming",
        CardioType.HIIT: "HIIT",
        CardioType.SPORTS: "Sport",
        CardioType.OTHER: "Something else",
    }
    return [{"value": key.value, "label": label} for key, label in labels.items()]
