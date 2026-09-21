"""
Media storage — the single choke point for every uploaded byte.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import secrets
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("storage")

CLOUD_PREFIX = "cld:"

ResourceType = Literal["image", "video"]
DeliveryType = Literal["upload", "authenticated"]

_deleter = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-delete")


# =============================================================================
# Backend selection
# =============================================================================


@lru_cache
def _cloudinary():
    """Configure and return the Cloudinary SDK, or None when not in use."""
    if not settings.use_cloudinary:
        return None

    import cloudinary  # noqa: PLC0415 — optional at import time for local dev
    import cloudinary.api  # noqa: F401,PLC0415
    import cloudinary.uploader  # noqa: F401,PLC0415
    import cloudinary.utils  # noqa: F401,PLC0415

    credentials = settings.cloudinary_credentials
    if credentials is None:  # pragma: no cover — use_cloudinary guarantees it
        return None
    cloud_name, api_key, api_secret = credentials
    # Always explicit. Never `cloudinary_url=`: the SDK does not parse that
    # keyword, it only parses CLOUDINARY_URL from os.environ at import time.
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return cloudinary


def backend_name() -> str:
    return "cloudinary" if _cloudinary() is not None else "local"


def describe() -> dict[str, str]:
    """Safe-to-log summary of the active backend (no secrets)."""
    if _cloudinary() is None:
        return {"backend": "local", "upload_dir": settings.UPLOAD_DIR}
    summary = {
        "backend": "cloudinary",
        "cloud": _cloudinary().config().cloud_name or "?",
        "folder": settings.CLOUDINARY_FOLDER,
        "signed_url_expiry": "on" if settings.cloudinary_auth_token_key else "off",
    }
    if settings.CLOUDINARY_AUTH_TOKEN_KEY and not settings.cloudinary_auth_token_key:
        summary["warning"] = (
            "CLOUDINARY_AUTH_TOKEN_KEY ignored: it is not a token-auth key "
            "(that is a separate hex key, not the API key). Remove it."
        )
    return summary


def verify() -> tuple[bool, str]:
    """Ask Cloudinary whether these credentials work. Returns (ok, message).

    Synchronous (one Admin API call). Used at startup in the background and by
    `python -m app.cli healthcheck`, so a wrong key shows up in the logs
    immediately — not as a vague error on the coach's first upload.
    """
    sdk = _cloudinary()
    if sdk is None:
        return True, f"local disk ({settings.UPLOAD_DIR}) — not durable, development only"
    try:
        sdk.api.ping(timeout=10)
    except Exception as exc:  # noqa: BLE001 — report whatever the SDK raised
        return False, _explain(exc)
    return True, f"Cloudinary cloud '{sdk.config().cloud_name}' accepted the credentials"


def _explain(exc: Exception) -> str:
    """A human sentence for a Cloudinary failure, for logs and the CLI."""
    name = type(exc).__name__
    text = str(exc)
    if "api_key" in text or "api_secret" in text or "cloud_name" in text:
        return f"credentials missing from the SDK configuration ({text})"
    if name == "AuthorizationRequired" or "Invalid Signature" in text or "Unknown API key" in text:
        return f"Cloudinary rejected the credentials — check the API key/secret ({text})"
    if name == "NotFound" and "cloud" in text.lower():
        return f"unknown cloud name ({text})"
    return f"{name}: {text}"


# =============================================================================
# Keys
# =============================================================================


@dataclass(frozen=True, slots=True)
class CloudKey:
    resource_type: ResourceType
    delivery_type: DeliveryType
    public_id: str

    def encode(self) -> str:
        return f"{CLOUD_PREFIX}{self.resource_type}:{self.delivery_type}:{self.public_id}"


def is_remote(key: str | None) -> bool:
    return bool(key) and key.startswith(CLOUD_PREFIX)


def parse_key(key: str) -> CloudKey | None:
    if not is_remote(key):
        return None
    try:
        resource_type, delivery_type, public_id = key[len(CLOUD_PREFIX) :].split(":", 2)
    except ValueError:
        return None
    if resource_type not in ("image", "video") or delivery_type not in ("upload", "authenticated"):
        return None
    if not public_id:
        return None
    return CloudKey(resource_type, delivery_type, public_id)  # type: ignore[arg-type]


def _public_id(*parts: str) -> str:
    folder = settings.CLOUDINARY_FOLDER.strip("/")
    tail = "/".join(str(part).strip("/") for part in parts if str(part))
    return f"{folder}/{tail}" if folder else tail


# =============================================================================
# Validation helpers (shared by both backends)
# =============================================================================


def _root() -> Path:
    path = Path(settings.UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guard_image_type(upload: UploadFile) -> None:
    """Reject the obviously-wrong file before reading it. Pillow is the real check."""
    if upload.content_type not in settings.ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG or WebP image.",
        )


async def _read_within_limit(upload: UploadFile, limit_mb: int) -> bytes:
    raw = await upload.read()
    if len(raw) > limit_mb * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"That image is over {limit_mb} MB. Try a smaller one.",
        )
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="That file was empty.")
    return raw


def _normalise(raw: bytes, *, max_side: int, quality: int) -> tuple[bytes, int, int]:
    """
    Decode, orient, strip metadata, downscale, re-encode as JPEG.

    `exif_transpose` runs before EXIF is dropped — phone cameras record
    orientation in EXIF rather than rotating pixels. Converting to RGB drops
    every EXIF field, GPS coordinates included.
    """
    try:
        with Image.open(BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.LANCZOS)
            out = BytesIO()
            image.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            return out.getvalue(), image.width, image.height
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="That file could not be read as an image."
        ) from exc


def _storage_unavailable(exc: Exception) -> HTTPException:
    """Map a Cloudinary failure to an honest status and message.

    * Misconfiguration (missing/rejected credentials) is the server's fault,
      not the file's: 503, and the coach is told it is a setup problem.
    * Cloudinary refusing the file itself (corrupt, unsupported codec, over the
      plan's size limit): 422 with Cloudinary's reason.
    * Anything else (network, timeout, rate limit): 502, try again.

    The full reason is always logged. In DEBUG it is also returned, so local
    development never has to go digging in the terminal.
    """
    reason = _explain(exc)
    name = type(exc).__name__
    log.error("storage.remote_failed", error=reason)
    suffix = f" ({reason})" if settings.DEBUG else ""

    if name == "AuthorizationRequired" or "Must supply" in str(exc):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage is not configured correctly, so uploads are paused. "
            f"This is a server setting, not your file.{suffix}",
        )
    if name == "BadRequest":
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The media service could not process that file: {exc}",
        )
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail=f"The media service did not respond properly. Try again in a moment.{suffix}",
    )


# =============================================================================
# Writing
# =============================================================================


async def _store_image(
    data: bytes,
    *,
    local_dir: tuple[str, ...],
    filename: str,
    delivery: DeliveryType,
) -> str:
    """Persist already-normalised JPEG bytes. Returns the storage key."""
    sdk = _cloudinary()
    if sdk is None:
        directory = _root().joinpath(*local_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(data)
        return "/".join((*local_dir, filename))

    public_id = _public_id(*local_dir, Path(filename).stem)
    try:
        await asyncio.to_thread(
            sdk.uploader.upload,
            BytesIO(data),
            resource_type="image",
            type=delivery,
            public_id=public_id,
            overwrite=False,
            unique_filename=False,
            use_filename=False,
            invalidate=True,
            timeout=settings.CLOUDINARY_UPLOAD_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — SDK raises a zoo of types
        raise _storage_unavailable(exc) from exc
    return CloudKey("image", delivery, public_id).encode()


async def save_progress_photo(
    client_id: uuid.UUID, upload: UploadFile, on_date: date
) -> tuple[str, str, int]:
    """Validate, strip metadata, downscale and store. Returns (key, type, bytes)."""
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)
    return await save_progress_photo_bytes(client_id, raw, on_date)


async def save_progress_photo_bytes(
    client_id: uuid.UUID, raw: bytes, on_date: date
) -> tuple[str, str, int]:
    """Same as `save_progress_photo`, from bytes already in memory (seeding)."""
    data, _, _ = _normalise(raw, max_side=1600, quality=85)
    # Local keys keep their historic `<client>/<date>/<file>` shape so rows
    # written before this change still resolve; Cloudinary gets a folder.
    local_dir = (str(client_id), on_date.isoformat())
    if is_cloud_enabled():
        local_dir = ("progress", *local_dir)
    key = await _store_image(
        data,
        local_dir=local_dir,
        filename=f"{secrets.token_urlsafe(12)}.jpg",
        delivery="authenticated",
    )
    return key, "image/jpeg", len(data)


async def save_message_image(
    sender_id: uuid.UUID, upload: UploadFile
) -> tuple[str, str, int, int, int]:
    """Store a message image. Returns (key, content_type, bytes, width, height).

    The client's filename never becomes part of the key — it is attacker
    controlled input and is recorded in the database for display only.
    """
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_MESSAGE_IMAGE_MB)
    data, width, height = _normalise(raw, max_side=1600, quality=82)
    key = await _store_image(
        data,
        local_dir=("messages", str(sender_id), date.today().isoformat()),
        filename=f"{secrets.token_urlsafe(16)}.jpg",
        delivery="authenticated",
    )
    return key, "image/jpeg", len(data), width, height


async def save_gallery_image(upload: UploadFile) -> tuple[str, int, int, int]:
    """Store a public gallery image. Returns (key, bytes, width, height).

    Dimensions come back so the public page can reserve layout space before the
    bytes land (no Cumulative Layout Shift on the page the client wants ranking).
    """
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)
    data, width, height = _normalise(raw, max_side=1800, quality=84)
    key = await _store_image(
        data,
        local_dir=("gallery",),
        filename=f"{secrets.token_urlsafe(14)}.jpg",
        delivery="upload",
    )
    return key, len(data), width, height


async def save_tutorial_poster(upload: UploadFile) -> tuple[str, int, int, int]:
    """Store a tutorial poster frame. Returns (key, bytes, width, height)."""
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)
    data, width, height = _normalise(raw, max_side=960, quality=80)
    key = await _store_image(
        data,
        local_dir=("tutorials", "posters"),
        filename=f"{secrets.token_urlsafe(14)}.jpg",
        delivery="authenticated",
    )
    return key, len(data), width, height


async def save_program_image(program_id: uuid.UUID, upload: UploadFile) -> tuple[str, int]:
    """Store a pricing plan's hero image (public marketing artwork). Returns (key, bytes)."""
    _guard_image_type(upload)
    raw = await _read_within_limit(upload, settings.MAX_UPLOAD_MB)
    data, _, _ = _normalise(raw, max_side=1400, quality=86)
    key = await _store_image(
        data,
        local_dir=("programs",),
        filename=f"{program_id}-{secrets.token_urlsafe(8)}.jpg",
        delivery="upload",
    )
    return key, len(data)


VIDEO_CHUNK = 1024 * 1024  # 1 MB
VIDEO_SUFFIXES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}


async def save_tutorial_video(upload: UploadFile) -> tuple[str, str, int]:
    """Stream an uploaded video to storage. Returns (key, content_type, bytes).

    The upload is spooled to a temporary file in 1 MB chunks (never held in
    worker memory), size-checked as it arrives, and — on Cloudinary — sent with
    the chunked `upload_large` API so files above the plain-upload ceiling work.
    """
    if upload.content_type not in settings.ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload an MP4, MOV or WebM video.",
        )

    suffix = VIDEO_SUFFIXES.get(upload.content_type, ".mp4")
    filename = f"{secrets.token_urlsafe(16)}{suffix}"
    limit = settings.MAX_VIDEO_UPLOAD_MB * 1024 * 1024
    sdk = _cloudinary()

    if sdk is None:
        directory = _root() / "tutorials"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
    else:
        spool_dir = Path(tempfile.gettempdir())
        destination = spool_dir / f"coach-auto-{filename}"

    written = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(VIDEO_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"That video is over {settings.MAX_VIDEO_UPLOAD_MB} MB. "
                        "Compress it, or host it and paste the link instead.",
                    )
                handle.write(chunk)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="That file was empty.")

        if sdk is None:
            return f"tutorials/{filename}", upload.content_type, written

        public_id = _public_id("tutorials", Path(filename).stem)
        try:
            await asyncio.to_thread(
                sdk.uploader.upload_large,
                str(destination),
                resource_type="video",
                type="authenticated",
                public_id=public_id,
                overwrite=False,
                chunk_size=20 * 1024 * 1024,
                timeout=settings.CLOUDINARY_UPLOAD_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            raise _storage_unavailable(exc) from exc
        return CloudKey("video", "authenticated", public_id).encode(), upload.content_type, written
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="That video could not be saved."
        ) from exc
    finally:
        # The spool file is only ever temporary on the Cloudinary path.
        if sdk is not None:
            destination.unlink(missing_ok=True)


# =============================================================================
# Reading
# =============================================================================


def is_cloud_enabled() -> bool:
    return _cloudinary() is not None


def resolve_path(key: str, *, not_found_message: str = "That file could not be found.") -> Path:
    """Resolve a *local* key to a path, refusing anything that escapes the root."""
    if not key or is_remote(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)

    root = _root().resolve()
    candidate = (root / key).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)
    return candidate


def exists(key: str | None) -> bool:
    """Cheap existence check. Remote keys are trusted when well-formed and in our folder."""
    if not key:
        return False
    parsed = parse_key(key)
    if parsed is not None:
        folder = settings.CLOUDINARY_FOLDER.strip("/")
        return not folder or parsed.public_id.startswith(f"{folder}/")
    try:
        resolve_path(key)
    except HTTPException:
        return False
    return True


def ensure_exists(key: str, *, not_found_message: str = "That file could not be found.") -> None:
    if not exists(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)


def _delivery_url(parsed: CloudKey, *, transformation: list[dict] | None = None) -> str:
    sdk = _cloudinary()
    if sdk is None:  # a cloud key on a local deployment — cannot be served
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That file could not be found.")

    options: dict = {
        "resource_type": parsed.resource_type,
        "type": parsed.delivery_type,
        "secure": True,
    }
    if parsed.resource_type == "image" and not transformation:
        # Private originals are always stored as JPEG. Public renditions omit
        # the extension so `f_auto` can serve AVIF/WebP to browsers that take it.
        options["format"] = "jpg"
    if transformation:
        options["transformation"] = transformation
    if parsed.delivery_type == "authenticated":
        options["sign_url"] = True
        if settings.cloudinary_auth_token_key:
            ttl = (
                settings.MEDIA_VIDEO_URL_TTL_SECONDS
                if parsed.resource_type == "video"
                else settings.MEDIA_URL_TTL_SECONDS
            )
            options["auth_token"] = {"key": settings.cloudinary_auth_token_key, "duration": ttl}
    url, _ = sdk.utils.cloudinary_url(parsed.public_id, **options)
    return url


def public_url(key: str | None, *, width: int | None = None) -> str | None:
    """
    A direct CDN address for a *public* asset, or None.

    None means "serve it through the API route instead" — the key is local, or
    the asset is private. `width` asks the CDN for a resized rendition;
    `f_auto,q_auto` lets it pick AVIF/WebP and a sensible quality per browser.
    """
    parsed = parse_key(key or "")
    if parsed is None or parsed.delivery_type != "upload" or _cloudinary() is None:
        return None
    transformation: list[dict] = [{"fetch_format": "auto", "quality": "auto"}]
    if width:
        transformation.insert(0, {"width": width, "crop": "limit"})
    return _delivery_url(parsed, transformation=transformation)


def serve(
    key: str | None,
    *,
    not_found_message: str,
    media_type: str | None = None,
    cache_control: str = "private, max-age=300",
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """
    Answer a media request for `key` — after the caller has authorised it.

    Remote: a 307 to a (signed, for private assets) Cloudinary URL. The
    browser follows it transparently for <img> and <video>, and Cloudinary
    honours Range requests, so seeking a tutorial still works.
    Local: stream the file from disk as before.
    """
    headers = {"Cache-Control": cache_control, **(extra_headers or {})}
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_message)

    parsed = parse_key(key)
    if parsed is not None:
        return RedirectResponse(
            _delivery_url(parsed), status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers=headers
        )

    path = resolve_path(key, not_found_message=not_found_message)
    guessed = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=guessed, headers=headers)


# =============================================================================
# Deleting
# =============================================================================


def _destroy_remote(parsed: CloudKey) -> bool:
    sdk = _cloudinary()
    if sdk is None:
        return False
    try:
        result = sdk.uploader.destroy(
            parsed.public_id,
            resource_type=parsed.resource_type,
            type=parsed.delivery_type,
            invalidate=True,
        )
        return (result or {}).get("result") in ("ok", "not found")
    except Exception as exc:  # noqa: BLE001 — deleting is best effort
        log.warning("storage.remote_delete_failed", public_id=parsed.public_id, error=str(exc))
        return False


def delete_file(key: str | None) -> None:
    """Best-effort delete. Never raises; remote deletes run in the background."""
    if not key:
        return
    parsed = parse_key(key)
    if parsed is not None:
        _deleter.submit(_destroy_remote, parsed)
        return
    with contextlib.suppress(HTTPException):
        resolve_path(key).unlink(missing_ok=True)


def delete_now(key: str | None) -> bool:
    """Synchronous delete for CLI tooling. Returns whether something was removed."""
    if not key:
        return False
    parsed = parse_key(key)
    if parsed is not None:
        return _destroy_remote(parsed)
    try:
        resolve_path(key).unlink(missing_ok=True)
        return True
    except HTTPException:
        return False


def purge_remote_folder() -> int:
    """Delete every asset under CLOUDINARY_FOLDER. Only for `reset --scope all`."""
    sdk = _cloudinary()
    folder = settings.CLOUDINARY_FOLDER.strip("/")
    if sdk is None or not folder:
        return 0
    removed = 0
    for resource_type in ("image", "video"):
        for delivery in ("upload", "authenticated"):
            try:
                while True:
                    result = sdk.api.delete_resources_by_prefix(
                        f"{folder}/", resource_type=resource_type, type=delivery
                    )
                    deleted = result.get("deleted") or {}
                    removed += sum(1 for status_ in deleted.values() if status_ == "deleted")
                    if not result.get("partial"):
                        break
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "storage.purge_failed",
                    resource_type=resource_type,
                    type=delivery,
                    error=str(exc),
                )
    return removed


# =============================================================================
# Migrating local files to Cloudinary (CLI: `media-migrate`)
# =============================================================================

_LOCAL_DELIVERY: dict[str, tuple[ResourceType, DeliveryType]] = {
    "gallery": ("image", "upload"),
    "programs": ("image", "upload"),
    "messages": ("image", "authenticated"),
    "tutorials/posters": ("image", "authenticated"),
    "tutorials": ("video", "authenticated"),
}


def migrate_local_key(key: str) -> str | None:
    """
    Upload one still-present local file to Cloudinary and return its new key.

    Returns None when the file is already gone (nothing to migrate) or when
    Cloudinary is not configured. Synchronous — CLI use only.
    """
    sdk = _cloudinary()
    if sdk is None or not key or is_remote(key):
        return None
    try:
        path = resolve_path(key)
    except HTTPException:
        return None

    resource_type: ResourceType = "image"
    delivery: DeliveryType = "authenticated"  # anything unrecognised stays private
    for prefix, (rtype, dtype) in _LOCAL_DELIVERY.items():
        if key.startswith(prefix + "/"):
            resource_type, delivery = rtype, dtype
            break

    public_id = _public_id(
        "progress"
        if delivery == "authenticated" and not key.startswith(("messages/", "tutorials/"))
        else "",
        str(Path(key).with_suffix("")),
    )
    uploader = sdk.uploader.upload_large if resource_type == "video" else sdk.uploader.upload
    uploader(
        str(path),
        resource_type=resource_type,
        type=delivery,
        public_id=public_id,
        overwrite=True,
        timeout=settings.CLOUDINARY_UPLOAD_TIMEOUT,
    )
    return CloudKey(resource_type, delivery, public_id).encode()