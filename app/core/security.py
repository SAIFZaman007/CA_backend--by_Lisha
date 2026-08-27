"""Password hashing (Argon2id) and JWT issuing/verification."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

# Argon2id — the current OWASP recommendation for new applications.
password_hasher = PasswordHash.recommended()

TokenType = Literal["access", "refresh", "reset"]


def hash_password(plain: str) -> str:
    return password_hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return password_hasher.verify(plain, hashed)
    except Exception:
        return False


def needs_rehash(hashed: str) -> bool:
    return password_hasher.verify_and_update("", hashed)[1] is not None


def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta, **extra: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
        "jti": str(uuid.uuid4()),
        **extra,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID | str, role: str) -> str:
    return _create_token(
        str(user_id),
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        role=role,
    )


def create_refresh_token(user_id: uuid.UUID | str, audience: str) -> tuple[str, datetime]:
    """`audience` is either `"staff"` or `"client"` — see `refresh_audience` in
    `app.api.v1.endpoints.auth`.

    This is the second half of session isolation between the two frontends.
    The cookie *name* already keeps a client's cookie from physically landing
    in the coach dashboard's cookie slot (see `REFRESH_COOKIE_NAMES` in
    `auth.py`), but a cookie name is just a label the browser attaches — it is
    not cryptographically bound to anything. Embedding the audience inside the
    signed token itself means that even if a cookie somehow ended up on the
    wrong path or domain (a misconfigured `COOKIE_DOMAIN` in a future
    deployment, a proxy that rewrites paths, a browser bug), the token it
    carries still will not decode as valid for the endpoint reading it. Two
    independent locks rather than one.
    """
    expires_at = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token = _create_token(
        str(user_id),
        "refresh",
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        aud=audience,
    )
    return token, expires_at


def create_reset_token(user_id: uuid.UUID | str, password_hash: str) -> str:
    """Reset tokens embed a fingerprint of the current password hash so a token
    is invalidated the moment the password changes or is reused."""
    return _create_token(
        str(user_id),
        "reset",
        timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
        fp=fingerprint(password_hash),
    )


def decode_token(
    token: str, expected_type: TokenType, *, expected_audience: str | None = None
) -> dict[str, Any] | None:
    """`expected_audience` matters for more than just filtering afterwards.

    PyJWT treats `aud` as a registered claim: if a token carries one, calling
    `jwt.decode()` without an explicit `audience=` argument makes PyJWT
    validate it against *nothing*, which always fails with
    `InvalidAudienceError` rather than silently ignoring the claim. Refresh
    tokens carry `aud`; access and reset tokens do not. So this has to either
    pass the audience through for PyJWT to check, or explicitly turn the
    check off for token types that were never given one — checking
    `payload.get("aud")` against `expected_audience` afterwards, as a plain
    dict comparison, never runs if the decode call above it raises first.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=expected_audience,
            options={"verify_aud": expected_audience is not None},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload


def fingerprint(value: str) -> str:
    """Short, non-reversible fingerprint used for reset tokens."""
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def hash_token(token: str) -> str:
    """Refresh tokens are stored hashed so a database leak cannot be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


# --- Signed media URLs --------------------------------------------------------

MEDIA_AUDIENCE = "media"


def sign_media_url(resource_id: uuid.UUID, viewer_id: uuid.UUID, ttl_seconds: int) -> str:
    """A token granting one viewer temporary read access to one file."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(viewer_id),
        "res": str(resource_id),
        "typ": MEDIA_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_media_token(token: str, resource_id: uuid.UUID) -> uuid.UUID | None:
    """Return the viewer this token was minted for, or None if it is not valid.

    Binding the signature to the resource id matters: without it, a token for
    one photo would open every photo the same viewer could reach.
    """
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None

    if claims.get("typ") != MEDIA_AUDIENCE or claims.get("res") != str(resource_id):
        return None
    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        return None