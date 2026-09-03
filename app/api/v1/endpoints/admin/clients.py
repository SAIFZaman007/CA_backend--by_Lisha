"""
The client roster and one client's whole record.

`GET /admin/clients/{id}` deliberately returns everything the coach needs on the
client screen in one round trip. Six separate requests would render the page in
six stages and make the dashboard feel slow on a gym wi-fi connection.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select

from app.core.deps import CurrentAdmin, CurrentCoach, DbSession
from app.core.logging import get_logger
from app.core.media import api_path, media_url
from app.core.security import hash_password
from app.models.engagement import Message, MessageThread
from app.models.enums import SessionStatus, TrainingLevel, UserRole
from app.models.nutrition import MealPlan
from app.models.tracking import BodyMeasurement, CardioLog, ProgressPhoto, SleepLog, WeightLog
from app.models.training import SetLog, WorkoutDay, WorkoutPlan, WorkoutSession
from app.models.user import ClientProfile, User
from app.schemas.admin import (
    CardioRow,
    ClientAccountUpdate,
    ClientCreate,
    ClientDetail,
    ClientPage,
    ClientRow,
    CoachProfileUpdate,
    MeasurementRow,
    MetricPoint,
    PhotoRow,
    SessionRow,
    SleepRow,
)
from app.services import reports, storage
from app.services.email import send_welcome

router = APIRouter(prefix="/clients")
log = get_logger("admin.clients")


async def _load_client(db: DbSession, client_id: uuid.UUID) -> User:
    user = await db.get(User, client_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That client was not found.")
    return user


async def _ensure_profile(db: DbSession, user: User) -> ClientProfile:
    if user.profile is None:
        profile = ClientProfile(user_id=user.id)
        db.add(profile)
        await db.flush()
        await db.refresh(user, ["profile"])
        return profile
    return user.profile


def _row(user: User, profile: ClientProfile | None, **derived) -> ClientRow:
    return ClientRow(
        id=user.id,
        full_name=user.full_name,
        display_name=user.display_name,
        email=user.email,
        avatar_url=user.avatar_url,
        is_active=user.is_active,
        role=user.role,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        level=profile.level if profile else None,
        goal=profile.goal if profile else None,
        phase=profile.phase if profile else None,
        program_week=profile.program_week if profile else None,
        program_total_weeks=profile.program_total_weeks if profile else None,
        current_weight_kg=float(profile.current_weight_kg)
        if profile and profile.current_weight_kg is not None
        else None,
        starting_weight_kg=float(profile.starting_weight_kg)
        if profile and profile.starting_weight_kg is not None
        else None,
        goal_weight_kg=float(profile.goal_weight_kg)
        if profile and profile.goal_weight_kg is not None
        else None,
        onboarding_completed=profile.onboarding_completed if profile else False,
        **derived,
    )


# --- Roster -------------------------------------------------------------------


@router.get("", response_model=ClientPage)
async def list_clients(
    coach: CurrentCoach,
    db: DbSession,
    search: str | None = Query(None, max_length=120),
    level: TrainingLevel | None = None,
    status_filter: str = Query("all", pattern="^(all|active|inactive|needs_attention)$"),
    sort: str = Query("recent", pattern="^(recent|name|level|last_session)$"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ClientPage:
    today = date.today()
    week_ago = today - timedelta(days=7)

    # Pre-aggregate the per-client signals so the list stays one query, not N+1.
    last_weight = (
        select(WeightLog.client_id.label("cid"), func.max(WeightLog.log_date).label("v"))
        .group_by(WeightLog.client_id)
        .subquery()
    )
    last_session = (
        select(
            WorkoutSession.client_id.label("cid"),
            func.max(WorkoutSession.session_date).label("v"),
        )
        .group_by(WorkoutSession.client_id)
        .subquery()
    )
    week_sessions = (
        select(WorkoutSession.client_id.label("cid"), func.count(WorkoutSession.id).label("v"))
        .where(
            WorkoutSession.session_date >= week_ago,
            WorkoutSession.status == SessionStatus.COMPLETED,
        )
        .group_by(WorkoutSession.client_id)
        .subquery()
    )
    unread = (
        select(MessageThread.client_id.label("cid"), func.count(Message.id).label("v"))
        .join(Message, Message.thread_id == MessageThread.id)
        .where(Message.read_at.is_(None), Message.sender_id == MessageThread.client_id)
        .group_by(MessageThread.client_id)
        .subquery()
    )
    plan_flag = (
        select(WorkoutPlan.client_id.label("cid"), func.count(WorkoutPlan.id).label("v"))
        .where(WorkoutPlan.is_active.is_(True))
        .group_by(WorkoutPlan.client_id)
        .subquery()
    )
    meal_flag = (
        select(MealPlan.client_id.label("cid"), func.count(MealPlan.id).label("v"))
        .where(MealPlan.is_active.is_(True))
        .group_by(MealPlan.client_id)
        .subquery()
    )

    base = (
        select(
            User,
            ClientProfile,
            last_weight.c.v,
            last_session.c.v,
            func.coalesce(week_sessions.c.v, 0),
            func.coalesce(unread.c.v, 0),
            func.coalesce(plan_flag.c.v, 0),
            func.coalesce(meal_flag.c.v, 0),
        )
        .outerjoin(ClientProfile, ClientProfile.user_id == User.id)
        .outerjoin(last_weight, last_weight.c.cid == User.id)
        .outerjoin(last_session, last_session.c.cid == User.id)
        .outerjoin(week_sessions, week_sessions.c.cid == User.id)
        .outerjoin(unread, unread.c.cid == User.id)
        .outerjoin(plan_flag, plan_flag.c.cid == User.id)
        .outerjoin(meal_flag, meal_flag.c.cid == User.id)
        .where(User.role == UserRole.CLIENT)
    )

    if search:
        pattern = f"%{search.strip()}%"
        base = base.where(
            or_(
                User.full_name.ilike(pattern),
                User.display_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if level:
        base = base.where(ClientProfile.level == level)
    if status_filter == "active":
        base = base.where(User.is_active.is_(True))
    elif status_filter == "inactive":
        base = base.where(User.is_active.is_(False))
    elif status_filter == "needs_attention":
        base = base.where(
            User.is_active.is_(True),
            or_(last_weight.c.v.is_(None), last_weight.c.v < today - timedelta(days=10)),
        )

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    order = {
        "recent": User.created_at.desc(),
        "name": User.full_name.asc(),
        "level": ClientProfile.level.asc(),
        "last_session": last_session.c.v.desc().nullslast(),
    }[sort]

    rows = (await db.execute(base.order_by(order).limit(limit).offset(offset))).all()

    items = [
        _row(
            user,
            profile,
            last_weight_log=lw,
            last_session_date=ls,
            sessions_last_7d=week,
            unread_from_client=unread_count,
            has_active_plan=bool(plans),
            has_active_meal_plan=bool(meals),
        )
        for user, profile, lw, ls, week, unread_count, plans, meals in rows
    ]

    return ClientPage(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ClientRow, status_code=status.HTTP_201_CREATED)
async def create_client(payload: ClientCreate, admin: CurrentAdmin, db: DbSession) -> ClientRow:
    """Open an account on someone's behalf — the usual path after a consultation."""
    email = payload.email.lower().strip()
    taken = (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
    if taken:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="An account already uses that email address."
        )

    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        role=payload.role,
    )
    db.add(user)
    await db.flush()

    profile = ClientProfile(user_id=user.id, level=payload.level)
    db.add(profile)
    await db.flush()

    if payload.send_welcome:
        await send_welcome(user.email, user.full_name.split()[0])

    log.info("admin.client_created", client_id=str(user.id), by=str(admin.id))
    return _row(user, profile)


# --- One client ---------------------------------------------------------------


@router.get("/{client_id}", response_model=ClientDetail)
async def client_detail(
    client_id: uuid.UUID,
    coach: CurrentCoach,
    db: DbSession,
    days: int = Query(180, ge=14, le=730),
) -> ClientDetail:
    user = await _load_client(db, client_id)
    profile = await _ensure_profile(db, user)
    since = date.today() - timedelta(days=days)

    weights = (
        (
            await db.execute(
                select(WeightLog)
                .where(WeightLog.client_id == client_id, WeightLog.log_date >= since)
                .order_by(WeightLog.log_date)
            )
        )
        .scalars()
        .all()
    )
    measurements = (
        (
            await db.execute(
                select(BodyMeasurement)
                .where(BodyMeasurement.client_id == client_id)
                .order_by(BodyMeasurement.log_date.desc())
                .limit(24)
            )
        )
        .scalars()
        .all()
    )
    sleep = (
        (
            await db.execute(
                select(SleepLog)
                .where(SleepLog.client_id == client_id, SleepLog.log_date >= since)
                .order_by(SleepLog.log_date.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    cardio = (
        (
            await db.execute(
                select(CardioLog)
                .where(CardioLog.client_id == client_id, CardioLog.log_date >= since)
                .order_by(CardioLog.log_date.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    photos = (
        (
            await db.execute(
                select(ProgressPhoto)
                .where(
                    ProgressPhoto.client_id == client_id,
                    ProgressPhoto.shared_with_coach.is_(True),
                )
                .order_by(ProgressPhoto.log_date.desc())
                .limit(36)
            )
        )
        .scalars()
        .all()
    )

    session_rows = (
        await db.execute(
            select(
                WorkoutSession,
                WorkoutDay.label,
                WorkoutDay.focus,
                func.count(SetLog.id),
                func.coalesce(
                    func.sum(func.coalesce(SetLog.weight_kg, 0) * func.coalesce(SetLog.reps, 0)),
                    0,
                ),
            )
            .outerjoin(WorkoutDay, WorkoutDay.id == WorkoutSession.day_id)
            .outerjoin(SetLog, SetLog.session_id == WorkoutSession.id)
            .where(WorkoutSession.client_id == client_id, WorkoutSession.session_date >= since)
            .group_by(WorkoutSession.id, WorkoutDay.label, WorkoutDay.focus)
            .order_by(WorkoutSession.session_date.desc())
            .limit(60)
        )
    ).all()

    week_ago = date.today() - timedelta(days=7)
    completed_7d = sum(
        1
        for s, *_ in session_rows
        if s.session_date >= week_ago and s.status == SessionStatus.COMPLETED
    )
    cardio_7d = sum(c.duration_minutes for c in cardio if c.log_date >= week_ago)
    sleep_7d = [float(s.hours_slept) for s in sleep if s.log_date >= week_ago]

    adherence = {
        "sessions_last_7d": completed_7d,
        "workout_target": profile.weekly_workout_target,
        "cardio_minutes_last_7d": cardio_7d,
        "cardio_target": profile.weekly_cardio_target_min,
        "avg_sleep_last_7d": round(sum(sleep_7d) / len(sleep_7d), 1) if sleep_7d else None,
        "sleep_target": float(profile.sleep_target_hours),
        "weight_change_kg": (
            round(float(weights[-1].weight_kg) - float(weights[0].weight_kg), 2)
            if len(weights) > 1
            else None
        ),
        "last_weight_log": weights[-1].log_date.isoformat() if weights else None,
    }

    return ClientDetail(
        account=_row(
            user,
            profile,
            last_weight_log=weights[-1].log_date if weights else None,
            last_session_date=session_rows[0][0].session_date if session_rows else None,
            sessions_last_7d=completed_7d,
        ),
        profile={
            "date_of_birth": profile.date_of_birth.isoformat() if profile.date_of_birth else None,
            "sex": profile.sex.value if profile.sex else None,
            "height_cm": float(profile.height_cm) if profile.height_cm is not None else None,
            "starting_weight_kg": float(profile.starting_weight_kg)
            if profile.starting_weight_kg is not None
            else None,
            "current_weight_kg": float(profile.current_weight_kg)
            if profile.current_weight_kg is not None
            else None,
            "goal_weight_kg": float(profile.goal_weight_kg)
            if profile.goal_weight_kg is not None
            else None,
            "goal": profile.goal.value,
            "activity_level": profile.activity_level.value,
            "unit_system": profile.unit_system.value,
            "level": profile.level.value,
            "phase": profile.phase,
            "program_start_date": profile.program_start_date.isoformat()
            if profile.program_start_date
            else None,
            "program_week": profile.program_week,
            "program_total_weeks": profile.program_total_weeks,
            "calorie_target": profile.calorie_target,
            "protein_target_g": profile.protein_target_g,
            "carb_target_g": profile.carb_target_g,
            "fat_target_g": profile.fat_target_g,
            "weekly_workout_target": profile.weekly_workout_target,
            "sleep_target_hours": float(profile.sleep_target_hours),
            "weekly_cardio_target_min": profile.weekly_cardio_target_min,
            "timezone": profile.timezone,
            "medical_notes": profile.medical_notes,
            "coach_notes": profile.coach_notes,
            "onboarding_completed": profile.onboarding_completed,
        },
        adherence=adherence,
        weight_series=[
            MetricPoint(log_date=w.log_date, value=float(w.weight_kg)) for w in weights
        ],
        measurements=[MeasurementRow.model_validate(m) for m in measurements],
        sleep=[SleepRow.model_validate(s) for s in sleep],
        cardio=[
            CardioRow(
                id=c.id,
                log_date=c.log_date,
                activity_type=c.activity_type.value,
                duration_minutes=c.duration_minutes,
                distance_km=float(c.distance_km) if c.distance_km is not None else None,
                avg_heart_rate=c.avg_heart_rate,
                calories_burned=c.calories_burned,
                intensity=c.intensity.value,
            )
            for c in cardio
        ],
        sessions=[
            SessionRow(
                id=s.id,
                session_date=s.session_date,
                status=s.status.value,
                duration_minutes=s.duration_minutes,
                day_label=label,
                focus=focus,
                set_count=set_count,
                volume_kg=float(volume or 0),
            )
            for s, label, focus, set_count, volume in session_rows
        ],
        photos=[
            PhotoRow(
                id=p.id,
                log_date=p.log_date,
                pose=p.pose.value,
                url=media_url(
                    api_path("admin", "clients", str(client_id), "photos", str(p.id), "file")
                ),
                note=p.note,
            )
            for p in photos
        ],
    )


@router.patch("/{client_id}", response_model=ClientRow)
async def update_account(
    client_id: uuid.UUID, payload: ClientAccountUpdate, admin: CurrentAdmin, db: DbSession
) -> ClientRow:
    """Account-level changes — name, email, access, role. Admin only."""
    user = await _load_client(db, client_id)
    updates = payload.model_dump(exclude_unset=True)

    if "email" in updates and updates["email"]:
        email = updates["email"].lower().strip()
        clash = (
            await db.execute(select(User.id).where(User.email == email, User.id != client_id))
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="Another account already uses that email."
            )
        updates["email"] = email

    if user.id == admin.id and updates.get("is_active") is False:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="You cannot switch off your own account."
        )

    for field, value in updates.items():
        setattr(user, field, value)
    await db.flush()

    log.info("admin.account_updated", client_id=str(user.id), by=str(admin.id))
    return _row(user, user.profile)


@router.patch("/{client_id}/profile")
async def update_profile(
    client_id: uuid.UUID, payload: CoachProfileUpdate, coach: CurrentCoach, db: DbSession
) -> dict:
    """The coaching record: level, phase, macro targets, private notes."""
    user = await _load_client(db, client_id)
    profile = await _ensure_profile(db, user)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

    # First weight recorded becomes the baseline every later delta is measured from.
    if profile.starting_weight_kg is None and profile.current_weight_kg is not None:
        profile.starting_weight_kg = profile.current_weight_kg

    await db.flush()
    log.info("admin.profile_updated", client_id=str(client_id), by=str(coach.id))
    return {"status": "saved"}


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: uuid.UUID,
    admin: CurrentAdmin,
    db: DbSession,
    hard: bool = Query(False, description="Erase the record entirely instead of switching it off"),
) -> None:
    """Deactivating is the default and is reversible. `hard=true` erases the
    account and everything cascading from it — used for erasure requests."""
    user = await _load_client(db, client_id)
    if user.id == admin.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account."
        )

    if hard:
        await db.delete(user)
        log.warning("admin.client_erased", client_id=str(client_id), by=str(admin.id))
        return

    user.is_active = False
    log.info("admin.client_deactivated", client_id=str(client_id), by=str(admin.id))


@router.post("/{client_id}/reset-password")
async def force_password(
    client_id: uuid.UUID, payload: dict, admin: CurrentAdmin, db: DbSession
) -> dict:
    """Set a temporary password when a client cannot receive email."""
    from app.schemas.user import PASSWORD_RULES

    new_password = str(payload.get("password", ""))
    import re

    if (
        len(new_password) < 10
        or not re.search(r"[A-Z]", new_password)
        or not re.search(r"[a-z]", new_password)
        or not re.search(r"\d", new_password)
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=PASSWORD_RULES)

    user = await _load_client(db, client_id)
    user.hashed_password = hash_password(new_password)
    log.info("admin.password_forced", client_id=str(client_id), by=str(admin.id))
    return {"status": "saved"}


@router.get("/{client_id}/photos/{photo_id}/file")
async def client_photo(
    client_id: uuid.UUID, photo_id: uuid.UUID, coach: CurrentCoach, db: DbSession
) -> FileResponse:
    """Check-in photos are private files, never static assets. They are streamed
    through this authenticated route or not at all."""
    photo = await db.get(ProgressPhoto, photo_id)
    if photo is None or photo.client_id != client_id or not photo.shared_with_coach:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Photo not found.")

    return FileResponse(
        storage.resolve_path(photo.file_key, not_found_message="Photo not found."),
        media_type=photo.content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{client_id}/activity")
async def client_activity(
    client_id: uuid.UUID,
    coach: CurrentCoach,
    db: DbSession,
    limit: int = Query(30, ge=1, le=100),
) -> list[dict]:
    """A merged, newest-first feed of everything the client has done."""
    await _load_client(db, client_id)
    events: list[dict] = []

    for weight in (
        (
            await db.execute(
                select(WeightLog)
                .where(WeightLog.client_id == client_id)
                .order_by(WeightLog.log_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    ):
        events.append(
            {
                "at": weight.log_date.isoformat(),
                "kind": "weight",
                "detail": f"Logged {float(weight.weight_kg):.1f} kg",
            }
        )

    for session in (
        (
            await db.execute(
                select(WorkoutSession)
                .where(WorkoutSession.client_id == client_id)
                .order_by(WorkoutSession.session_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    ):
        events.append(
            {
                "at": session.session_date.isoformat(),
                "kind": "session",
                "detail": f"Training session {session.status.value.replace('_', ' ')}",
            }
        )

    for bout in (
        (
            await db.execute(
                select(CardioLog)
                .where(CardioLog.client_id == client_id)
                .order_by(CardioLog.log_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    ):
        events.append(
            {
                "at": bout.log_date.isoformat(),
                "kind": "cardio",
                "detail": f"{bout.duration_minutes} min {bout.activity_type.value}",
            }
        )

    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


@router.get("/{client_id}/export")
async def export_client(
    client_id: uuid.UUID,
    admin: CurrentAdmin,
    db: DbSession,
    format: Literal["csv", "pdf", "json"] = Query(
        "csv",
        description="`csv` and `pdf` are the two the dashboard's export button offers — a "
        "real file a coach can open or hand to someone. `json` is kept for genuine "
        "data-portability requests (GDPR/CCPA-style 'send me everything'), where a "
        "complete machine-readable copy is what is actually wanted.",
    ),
) -> Response:
    """
    One client's record, for download.

    `csv` and `pdf` are built from the same `ClientDetail` the dashboard's own
    client screen renders, in `app.services.reports` — so what a coach
    downloads can never show different numbers than what the screen showed
    when they clicked the button. `json` short-circuits to the old plain
    dict-as-JSON response, which FastAPI serialises for us automatically.
    """
    detail = await client_detail(client_id, admin, db, days=730)

    if format == "json":
        return JSONResponse(
            {
                "exported_at": datetime.now(UTC).isoformat(),
                "record": detail.model_dump(mode="json"),
            }
        )

    stem = reports.build_filename_stem(detail)
    if format == "csv":
        body = reports.build_client_csv(detail)
        media_type = "text/csv"
        filename = f"{stem}.csv"
    else:
        body = reports.build_client_pdf(detail)
        media_type = "application/pdf"
        filename = f"{stem}.pdf"

    log.info("admin.client_exported", client_id=str(client_id), format=format, by=str(admin.id))
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )