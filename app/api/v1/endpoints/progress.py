"""Weight, tape measurements and check-in photos."""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, OptionalUser
from app.core.security import sign_media_url, verify_media_token
from app.models.enums import PhotoPose
from app.models.tracking import BodyMeasurement, ProgressPhoto, WeightLog
from app.schemas.tracking import (
    MeasurementIn,
    MeasurementOut,
    ProgressPhotoOut,
    WeightLogIn,
    WeightLogOut,
)
from app.services import storage

router = APIRouter(prefix="/progress", tags=["progress"])


# --- Weight --------------------------------------------------------------------


@router.get("/weight", response_model=list[WeightLogOut])
async def list_weight(
    user: CurrentUser, db: DbSession, days: int = Query(180, ge=1, le=1825)
) -> list[WeightLog]:
    since = date.today() - timedelta(days=days)
    stmt = (
        select(WeightLog)
        .where(WeightLog.client_id == user.id, WeightLog.log_date >= since)
        .order_by(WeightLog.log_date)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.put("/weight", response_model=WeightLogOut)
async def log_weight(payload: WeightLogIn, user: CurrentUser, db: DbSession) -> WeightLog:
    """One weigh-in per day. Logging twice replaces the earlier entry."""
    target = payload.log_date or date.today()
    existing = (
        await db.execute(
            select(WeightLog).where(
                WeightLog.client_id == user.id, WeightLog.log_date == target
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.weight_kg = payload.weight_kg
        existing.note = payload.note
        row = existing
    else:
        row = WeightLog(
            client_id=user.id,
            log_date=target,
            weight_kg=payload.weight_kg,
            note=payload.note,
        )
        db.add(row)

    # Keep the profile in step so the dashboard and calculators agree.
    if user.profile is not None:
        user.profile.current_weight_kg = payload.weight_kg
        if user.profile.starting_weight_kg is None:
            user.profile.starting_weight_kg = payload.weight_kg
        db.add(user.profile)

    await db.flush()
    return row


# --- Measurements --------------------------------------------------------------


@router.get("/measurements", response_model=list[MeasurementOut])
async def list_measurements(
    user: CurrentUser, db: DbSession, limit: int = Query(24, ge=1, le=200)
) -> list[BodyMeasurement]:
    stmt = (
        select(BodyMeasurement)
        .where(BodyMeasurement.client_id == user.id)
        .order_by(BodyMeasurement.log_date.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.put("/measurements", response_model=MeasurementOut)
async def log_measurements(
    payload: MeasurementIn, user: CurrentUser, db: DbSession
) -> BodyMeasurement:
    """Chest/bust, waist and hips are the coach's assessment set. One row per day."""
    target = payload.log_date or date.today()
    data = payload.model_dump(exclude={"log_date"}, exclude_unset=True)

    existing = (
        await db.execute(
            select(BodyMeasurement).where(
                BodyMeasurement.client_id == user.id, BodyMeasurement.log_date == target
            )
        )
    ).scalar_one_or_none()

    if existing:
        for field, value in data.items():
            setattr(existing, field, value)
        await db.flush()
        return existing

    row = BodyMeasurement(client_id=user.id, log_date=target, **data)
    db.add(row)
    await db.flush()
    return row


# --- Photos --------------------------------------------------------------------
# Photos are private. They are never served as static files — only through the
# authenticated endpoint below, and only to the client who owns them.


def _photo_url(photo: ProgressPhoto, viewer_id: uuid.UUID) -> str:
    """A URL an <img> tag can actually load.

    The endpoint below accepts either a bearer token or this signature, so the
    same route serves both a scripted fetch and a plain image tag.
    """
    token = sign_media_url(photo.id, viewer_id, settings.MEDIA_URL_TTL_SECONDS)
    return f"/api/v1/progress/photos/{photo.id}/file?token={token}"


@router.get("/photos", response_model=list[ProgressPhotoOut])
async def list_photos(user: CurrentUser, db: DbSession) -> list[ProgressPhotoOut]:
    stmt = (
        select(ProgressPhoto)
        .where(ProgressPhoto.client_id == user.id)
        .order_by(ProgressPhoto.log_date.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        ProgressPhotoOut(
            id=p.id,
            log_date=p.log_date,
            pose=p.pose,
            url=_photo_url(p, user.id),
            note=p.note,
            shared_with_coach=p.shared_with_coach,
        )
        for p in rows
    ]


@router.post("/photos", response_model=ProgressPhotoOut, status_code=status.HTTP_201_CREATED)
async def upload_photo(
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = File(...),
    pose: PhotoPose = Form(PhotoPose.FRONT),
    log_date: date | None = Form(None),
    note: str | None = Form(None),
    shared_with_coach: bool = Form(True),
) -> ProgressPhotoOut:
    target = log_date or date.today()
    key, content_type, size = await storage.save_progress_photo(user.id, file, target)

    photo = ProgressPhoto(
        client_id=user.id,
        log_date=target,
        pose=pose,
        file_key=key,
        content_type=content_type,
        size_bytes=size,
        note=note,
        shared_with_coach=shared_with_coach,
    )
    db.add(photo)
    await db.flush()
    return ProgressPhotoOut(
        id=photo.id,
        log_date=photo.log_date,
        pose=photo.pose,
        url=_photo_url(photo, user.id),
        note=photo.note,
        shared_with_coach=photo.shared_with_coach,
    )


@router.get("/photos/{photo_id}/file")
async def get_photo_file(
    photo_id: uuid.UUID,
    db: DbSession,
    user: OptionalUser = None,
    token: str | None = Query(None),
) -> Response:
    """Serve one private photo.

    Authorised either by a bearer token (a scripted fetch) or by a signed
    `token` query parameter (an <img> tag, which cannot set headers). Whichever
    path is used, the resolved viewer must own the photo.
    """
    viewer_id = user.id if user else (verify_media_token(token, photo_id) if token else None)
    if viewer_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="This photo link has expired. Reload the page."
        )

    photo = (
        await db.execute(
            select(ProgressPhoto).where(
                ProgressPhoto.id == photo_id, ProgressPhoto.client_id == viewer_id
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Photo not found.")

    return FileResponse(
        storage.resolve_path(photo.file_key),
        media_type=photo.content_type,
        headers={"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex, noimageindex"},
    )


@router.delete("/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_photo(photo_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    photo = (
        await db.execute(
            select(ProgressPhoto).where(
                ProgressPhoto.id == photo_id, ProgressPhoto.client_id == user.id
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Photo not found.")
    storage.delete_file(photo.file_key)
    await db.delete(photo)


# --- Summary -------------------------------------------------------------------


@router.get("/summary")
async def progress_summary(
    user: CurrentUser, db: DbSession, days: int = Query(30, ge=7, le=365)
) -> dict:
    since = date.today() - timedelta(days=days)

    weights = (
        await db.execute(
            select(WeightLog)
            .where(WeightLog.client_id == user.id)
            .order_by(WeightLog.log_date)
        )
    ).scalars().all()

    measurements = (
        await db.execute(
            select(BodyMeasurement)
            .where(BodyMeasurement.client_id == user.id)
            .order_by(BodyMeasurement.log_date)
        )
    ).scalars().all()

    start_weight = float(weights[0].weight_kg) if weights else None
    current_weight = float(weights[-1].weight_kg) if weights else None
    window = [w for w in weights if w.log_date >= since]

    def _delta(field: str) -> dict | None:
        values = [(m.log_date, getattr(m, field)) for m in measurements if getattr(m, field)]
        if not values:
            return None
        first, latest = values[0][1], values[-1][1]
        return {
            "start_cm": float(first),
            "current_cm": float(latest),
            "change_cm": round(float(latest) - float(first), 1),
        }

    return {
        "start_weight_kg": start_weight,
        "current_weight_kg": current_weight,
        "goal_weight_kg": float(user.profile.goal_weight_kg)
        if user.profile and user.profile.goal_weight_kg
        else None,
        "total_change_kg": round(current_weight - start_weight, 2)
        if start_weight and current_weight
        else None,
        "window_change_kg": round(float(window[-1].weight_kg) - float(window[0].weight_kg), 2)
        if len(window) >= 2
        else None,
        "entries_logged": len(weights),
        "measurements": {
            field: _delta(field)
            for field in (
                "chest_cm",
                "waist_cm",
                "hips_cm",
                "left_arm_cm",
                "right_arm_cm",
                "left_thigh_cm",
            )
        },
        "program_week": user.profile.program_week if user.profile else 1,
        "program_total_weeks": user.profile.program_total_weeks if user.profile else 12,
    }