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
