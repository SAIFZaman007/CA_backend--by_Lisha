"""Local disk storage for progress photos.

Photos are private. Files are written outside the web root and served only
through an authenticated endpoint, never as static assets. Swap this module
for S3/R2 by keeping the same three function signatures.
"""

import secrets
import uuid
from datetime import date
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

MAX_DIMENSION = 1600


def _root() -> Path:
    path = Path(settings.UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def save_progress_photo(
    client_id: uuid.UUID, upload: UploadFile, on_date: date
) -> tuple[str, str, int]:
    """Validate, strip metadata, downscale and store. Returns (key, type, bytes)."""
    if upload.content_type not in settings.ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG or WebP image.",
        )

    raw = await upload.read()
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(raw) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"That image is over {settings.MAX_UPLOAD_MB} MB. Try a smaller one.",
        )

    directory = _root() / str(client_id) / on_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_urlsafe(12)}.jpg"
    destination = directory / filename

    try:
        from io import BytesIO

        with Image.open(BytesIO(raw)) as image:
            image = image.convert("RGB")  # drops EXIF, including GPS coordinates
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
            image.save(destination, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="That file could not be read as an image."
        ) from exc

    key = f"{client_id}/{on_date.isoformat()}/{filename}"
    return key, "image/jpeg", destination.stat().st_size


def resolve_path(key: str) -> Path:
    """Resolve a stored key to a path, refusing anything that escapes the root."""
    root = _root().resolve()
    candidate = (root / key).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Photo not found.")
    return candidate


def delete_file(key: str) -> None:
    try:
        resolve_path(key).unlink(missing_ok=True)
    except HTTPException:
        pass


# --- Coaching videos ----------------------------------------------------------
#
# The coach can either paste a hosting link or upload the file itself. Uploads
# stream to disk in chunks rather than being read into memory: a 500 MB lift
# demo read with `await upload.read()` would put 500 MB in the worker's heap and
# take the API down under two concurrent uploads.

VIDEO_CHUNK = 1024 * 1024  # 1 MB


async def save_tutorial_video(upload: UploadFile) -> tuple[str, str, int]:
    """Stream an uploaded video to disk. Returns (key, content_type, bytes)."""
    if upload.content_type not in settings.ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload an MP4, MOV or WebM video.",
        )

    directory = _root() / "tutorials"
    directory.mkdir(parents=True, exist_ok=True)

    suffix = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(upload.content_type, ".mp4")

    filename = f"{secrets.token_urlsafe(16)}{suffix}"
    destination = directory / filename
    limit = settings.MAX_VIDEO_UPLOAD_MB * 1024 * 1024
    written = 0

    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(VIDEO_CHUNK):
                written += len(chunk)
                if written > limit:
                    # Stop the moment the limit is passed and take the partial
                    # file with us — a rejected upload must not leave megabytes
                    # of orphaned data on the volume.
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"That video is over {settings.MAX_VIDEO_UPLOAD_MB} MB. "
                        "Compress it, or host it and paste the link instead.",
                    )
                handle.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="That video could not be saved."
        ) from exc

    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="That file was empty.")

    return f"tutorials/{filename}", upload.content_type, written


# --- Programme artwork --------------------------------------------------------
#
# Unlike check-in photos these are public marketing images, so they are
# downscaled harder and served without authentication.

PROGRAM_IMAGE_MAX = 1400


async def save_program_image(program_id: uuid.UUID, upload: UploadFile) -> tuple[str, int]:
    """Validate, downscale and store a tier's hero image. Returns (key, bytes)."""
    if upload.content_type not in settings.ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Upload a JPEG, PNG or WebP image."
        )

    raw = await upload.read()
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(raw) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"That image is over {settings.MAX_UPLOAD_MB} MB. Try a smaller one.",
        )

    directory = _root() / "programs"
    directory.mkdir(parents=True, exist_ok=True)
    # Keyed by program plus a random suffix: replacing artwork writes a new file
    # so a cached URL never serves the previous tier's photo.
    filename = f"{program_id}-{secrets.token_urlsafe(8)}.jpg"
    destination = directory / filename

    try:
        from io import BytesIO

        with Image.open(BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((PROGRAM_IMAGE_MAX, PROGRAM_IMAGE_MAX), Image.LANCZOS)
            image.save(destination, "JPEG", quality=86, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="That file could not be read as an image."
        ) from exc

    return f"programs/{filename}", destination.stat().st_size