"""Local disk storage for private and public media.

Photos are private. Files are written outside the web root and served only
through an authenticated endpoint, never as static assets. Swap this module
for S3/R2 by keeping the same function signatures.
"""

import secrets
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings

MAX_DIMENSION = 1600


def _root() -> Path:
    path = Path(settings.UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guard_image_type(upload: UploadFile) -> None:
    """Reject the wrong sort of file before reading a single byte of it.

    The declared content type is only a header and is not trusted on its own —
    `_normalise` decoding the bytes with Pillow is the real check. This just
    turns the common honest mistake (a PDF, a HEIC straight off an iPhone) into
    a clear sentence instead of a decode failure.
    """
    if upload.content_type not in settings.ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG or WebP image.",
        )


async def _read_within_limit(upload: UploadFile, limit_mb: int) -> bytes:
    raw = await upload.read()
    limit = limit_mb * 1024 * 1024
    if len(raw) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"That image is over {limit_mb} MB. Try a smaller one.",
        )
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="That file was empty.")
    return raw


def _normalise(raw: bytes, destination: Path, *, max_side: int, quality: int) -> tuple[int, int]:
    """Decode, strip metadata, orient, downscale, write JPEG. Returns (w, h).

    `exif_transpose` runs before the EXIF is dropped. Phone cameras record
    orientation in EXIF rather than rotating the pixels, so converting first
    and stripping second is how a portrait photo arrives on its side.
    """
    try:
        with Image.open(BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")  # drops EXIF, including GPS coordinates
            image.thumbnail((max_side, max_side), Image.LANCZOS)
            image.save(destination, "JPEG", quality=quality, optimize=True)
            return image.width, image.height
    except (UnidentifiedImageError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="That file could not be read as an image."
        ) from exc


async def save_progress_photo(
    client_id: uuid.UUID, upload: UploadFile, on_date: date
) -> tuple[str, str, int]:
    """Validate, strip metadata, downscale and store. Returns (key, type, bytes)."""
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)

    directory = _root() / str(client_id) / on_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_urlsafe(12)}.jpg"
    destination = directory / filename

    _normalise(raw, destination, max_side=MAX_DIMENSION, quality=85)

    key = f"{client_id}/{on_date.isoformat()}/{filename}"
    return key, "image/jpeg", destination.stat().st_size


def resolve_path(key: str, *, not_found_message: str = "That file could not be found.") -> Path:
    """Resolve a stored key to a path, refusing anything that escapes the root.

    `not_found_message` lets each caller phrase the 404 in the language of the
    thing it was actually serving — "That video could not be found." reads very
    differently to a coach than "Photo not found." does. Every caller in the
    codebase already passed this argument; the parameter being absent was a
    `TypeError` at request time on every route that served a file, which is
    what took tutorial streaming, check-in photos, gallery images and
    programme artwork down together.

    An empty or missing key is treated as not-found rather than resolving to
    the upload root itself, which `is_file()` would reject anyway but only
    after touching the filesystem.
    """
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)

    root = _root().resolve()
    candidate = (root / key).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)
    return candidate


def delete_file(key: str) -> None:
    try:
        resolve_path(key).unlink(missing_ok=True)
    except HTTPException:
        pass


# --- Message attachments ------------------------------------------------------
#
# A client photographing a loaded bar, a meal, or their setup mid-set. Private
# health-adjacent data: same treatment as a check-in photo — written under the
# sender's own directory, never served as a static file, only ever reachable
# through a signed, short-lived URL bound to one viewer and one attachment.

MESSAGE_IMAGE_MAX = 1600


async def save_message_image(
    sender_id: uuid.UUID, upload: UploadFile
) -> tuple[str, str, int, int, int]:
    """Store a message image. Returns (key, content_type, bytes, width, height).

    The client's filename is never used to build the path — it is recorded in
    the database for display only. A filename is attacker-controlled input, and
    the shortest route from "helpful, we kept their filename" to writing
    outside the upload root is treating one as a path component.
    """
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_MESSAGE_IMAGE_MB)

    directory = _root() / "messages" / str(sender_id) / date.today().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_urlsafe(16)}.jpg"
    destination = directory / filename

    width, height = _normalise(raw, destination, max_side=MESSAGE_IMAGE_MAX, quality=82)

    key = f"messages/{sender_id}/{date.today().isoformat()}/{filename}"
    return key, "image/jpeg", destination.stat().st_size, width, height


# --- Gallery ------------------------------------------------------------------
#
# Public marketing imagery — the Hall of the Coach. Unlike everything above,
# these are meant to be crawled and cached, so they are stored under a flat
# public prefix and served with long cache headers and no authentication.

GALLERY_IMAGE_MAX = 1800


async def save_gallery_image(upload: UploadFile) -> tuple[str, int, int, int]:
    """Store a gallery image. Returns (key, bytes, width, height).

    Dimensions come back because the public page needs them to reserve layout
    space before the bytes land. Without that the grid reflows as each image
    arrives, which is a Cumulative Layout Shift penalty on precisely the page
    the client wants ranking.
    """
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)

    directory = _root() / "gallery"
    directory.mkdir(parents=True, exist_ok=True)
    # Random rather than derived from the row id: replacing an image writes a
    # new file, so a URL cached at the edge for a year can never serve the
    # photo that used to be in that slot.
    filename = f"{secrets.token_urlsafe(14)}.jpg"
    destination = directory / filename

    width, height = _normalise(raw, destination, max_side=GALLERY_IMAGE_MAX, quality=84)
    return f"gallery/{filename}", destination.stat().st_size, width, height


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

PROGRAM_IMAGE_MAX = 1400


async def save_program_image(program_id: uuid.UUID, upload: UploadFile) -> tuple[str, int]:
    """Validate, downscale and store a tier's hero image. Returns (key, bytes)."""
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)

    directory = _root() / "programs"
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{program_id}-{secrets.token_urlsafe(8)}.jpg"
    destination = directory / filename

    _normalise(raw, destination, max_side=PROGRAM_IMAGE_MAX, quality=86)
    return f"programs/{filename}", destination.stat().st_size