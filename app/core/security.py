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


def create_refresh_token(user_id: uuid.UUID | str) -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token = _create_token(
        str(user_id), "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
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


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
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
