"""Registration, sign-in, token refresh and password reset.

Access tokens are short-lived and returned in the response body for the SPA to
hold in memory. Refresh tokens live in an HttpOnly, SameSite cookie and are
stored hashed, so they can be revoked server-side and cannot be read by script.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    fingerprint,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.enums import UserRole
from app.models.user import ClientProfile, RefreshSession, User
from app.schemas.user import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
)
from app.services.email import send_password_reset, send_welcome

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("auth")

# Two apps, two cookies. The client portal and the coach dashboard are
# separate frontends that both talk to this one API, and a session from one
# must never be readable as a session in the other.
REFRESH_COOKIE_NAMES: dict[str, str] = {
    "client": "coachauto_refresh_client",
    "staff": "coachauto_refresh_staff",
}
STAFF_ROLES = (UserRole.COACH, UserRole.ADMIN)

# Scoped so each cookie is only ever sent to the refresh and logout routes.
COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def refresh_audience(role: UserRole) -> str:
    """Which cookie slot a user's session belongs in. Always derived from the
    role on their own database row — never from anything the client sends —
    so there is no way to ask for the wrong audience's cookie."""
    return "staff" if role in STAFF_ROLES else "client"

# How long a just-rotated refresh token keeps working.
ROTATION_GRACE = timedelta(seconds=30)


def _set_refresh_cookie(response: Response, token: str, audience: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAMES[audience],
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path=COOKIE_PATH,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response, audience: str) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAMES[audience], path=COOKIE_PATH, domain=settings.COOKIE_DOMAIN
    )


def _clear_all_refresh_cookies(response: Response) -> None:
    """Used where we do not yet know which audience's cookie is stale (e.g. an
    unreadable or expired token on `/refresh`) — clear both rather than guess."""
    for audience in REFRESH_COOKIE_NAMES:
        _clear_refresh_cookie(response, audience)


async def _issue_session(
    db: DbSession,
    user: User,
    request: Request,
    response: Response,
    *,
    rotated_from: RefreshSession | None = None,
) -> TokenResponse:
    audience = refresh_audience(user.role)
    refresh_token, expires_at = create_refresh_token(user.id, audience)
    session = RefreshSession(
        user_id=user.id,
        token_hash=hash_token(refresh_token),
        expires_at=expires_at,
        user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        ip_address=request.client.host if request.client else None,
    )
    db.add(session)
    await db.flush()

    # Link the chain, so a replayed predecessor can be resolved to this row.
    if rotated_from is not None:
        rotated_from.replaced_by_id = session.id

    user.last_login_at = datetime.now(UTC)
    await db.flush()

    _set_refresh_cookie(response, refresh_token, audience)
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def _revoke_family(db: DbSession, session: RefreshSession) -> None:
    """Kill every session downstream of a reused token.

    Reuse outside the grace window means someone holds a copy they should not.
    We cannot tell the thief from the victim, so both are signed out and the
    person reauthenticates.
    """
    now = datetime.now(UTC)
    seen: set[uuid.UUID] = set()
    current: RefreshSession | None = session

    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.revoked_at is None:
            current.revoked_at = now
        current = (
            await db.get(RefreshSession, current.replaced_by_id)
            if current.replaced_by_id
            else None
        )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def register(
    request: Request, response: Response, payload: RegisterRequest, db: DbSession
) -> TokenResponse:
    email = payload.email.lower().strip()
    taken = (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
    if taken:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="An account already uses that email. Sign in instead, or reset your password.",
        )

    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        role=UserRole.CLIENT,
    )
    db.add(user)
    await db.flush()

    # Every client gets a profile row immediately so intake can be filled in later.
    db.add(ClientProfile(user_id=user.id))
    await db.flush()

    await send_welcome(user.email, user.full_name.split()[0])
    log.info("auth.registered", user_id=str(user.id))
    return await _issue_session(db, user, request, response)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(
    request: Request, response: Response, payload: LoginRequest, db: DbSession
) -> TokenResponse:
    email = payload.email.lower().strip()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    # Always run a verify to keep the timing of a wrong email and a wrong
    # password indistinguishable.
    stored = user.hashed_password if user else "$argon2id$v=19$m=65536,t=3,p=4$invalid$invalid"
    valid = verify_password(payload.password, stored)

    if not user or not valid:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="That email and password do not match."
        )
    if not user.is_active:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="This account is switched off. Email your coach to reopen it.",
        )

    log.info("auth.login", user_id=str(user.id))
    return await _issue_session(db, user, request, response)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    db: DbSession,
    audience: str = Query(
        "client",
        pattern="^(client|staff)$",
        description="Which app is asking: the client portal or the coach/admin dashboard. "
        "Selects which of the two isolated session cookies to read — see "
        "REFRESH_COOKIE_NAMES. Carries no privilege on its own: the session is still "
        "resolved from the signed, server-stored token underneath it.",
    ),
) -> TokenResponse:
    token = request.cookies.get(REFRESH_COOKIE_NAMES[audience])
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No active session.")

    # The audience is checked twice: once by which cookie we even looked at,
    # and again against the claim baked into the token itself. Belt and
    # braces — see the long comment above REFRESH_COOKIE_NAMES for why the
    # second check exists independently of the first.
    payload = decode_token(token, "refresh", expected_audience=audience)
    if payload is None:
        _clear_refresh_cookie(response, audience)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Your session has expired.")

    session = (
        await db.execute(
            select(RefreshSession).where(RefreshSession.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if session is None or session.expires_at <= now:
        _clear_refresh_cookie(response, audience)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Your session has expired.")

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response, audience)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Your session has expired.")

    # Defence in depth: even a token that decoded fine and matched a live,
    # unexpired session row must still belong to a user whose current role
    # actually maps to the audience it claims. A coach demoted to client
    # after this token was issued must not keep dashboard access on it.
    if refresh_audience(user.role) != audience:
        _clear_refresh_cookie(response, audience)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Your session has expired.")

    if session.revoked_at is not None:
        # Already rotated: either a harmless double-fire, or a replayed token.
        replacement = (
            await db.get(RefreshSession, session.replaced_by_id)
            if session.replaced_by_id
            else None
        )
        within_grace = now - session.revoked_at <= ROTATION_GRACE
        replacement_live = (
            replacement is not None
            and replacement.revoked_at is None
            and replacement.expires_at > now
        )

        if within_grace and replacement_live:
            # The winner of the race already holds a good token. Hand back a
            # matching access token rather than starting a third session, and
            # leave the cookie alone — it already carries the replacement.
            log.info("auth.refresh_raced", user_id=str(user.id))
            return TokenResponse(
                access_token=create_access_token(user.id, user.role.value),
                expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            )

        log.warning("auth.refresh_reuse", user_id=str(user.id), session_id=str(session.id))
        await _revoke_family(db, session)
        _clear_refresh_cookie(response, audience)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Your session was ended for security. Please sign in again.",
        )

    # Rotate: the old token dies the moment a new one is issued.
    session.revoked_at = now
    return await _issue_session(db, user, request, response, rotated_from=session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: DbSession) -> None:
    """Signs out whichever of the two session cookies this browser is holding.

    Both are checked, not just the one the calling app would normally use.
    Nothing stops someone having the client portal and the coach dashboard
    open in the same browser at once, each with its own cookie — a stray
    session in the other app is exactly the kind of loose end this endpoint
    exists to close, so both are revoked and cleared here regardless of which
    app's "Sign out" button was clicked.
    """
    for audience, cookie_name in REFRESH_COOKIE_NAMES.items():
        token = request.cookies.get(cookie_name)
        if token:
            session = (
                await db.execute(
                    select(RefreshSession).where(RefreshSession.token_hash == hash_token(token))
                )
            ).scalar_one_or_none()
            if session and session.revoked_at is None:
                session.revoked_at = datetime.now(UTC)
        _clear_refresh_cookie(response, audience)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def forgot_password(
    request: Request, response: Response, payload: ForgotPasswordRequest, db: DbSession
) -> dict[str, str]:
    email = payload.email.lower().strip()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user and user.is_active:
        await send_password_reset(user.email, create_reset_token(user.id, user.hashed_password))

    # Identical response either way — never reveal which emails have accounts.
    return {
        "message": "If that email has an account, a reset link is on its way. "
        "Check your spam folder if it does not arrive within a few minutes."
    }


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def reset_password(
    request: Request, response: Response, payload: ResetPasswordRequest, db: DbSession
) -> None:
    claims = decode_token(payload.token, "reset")
    if claims is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This reset link has expired or already been used. Request a new one.",
        )

    try:
        user = await db.get(User, uuid.UUID(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid reset link.") from exc

    if user is None or claims.get("fp") != fingerprint(user.hashed_password):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This reset link has expired or already been used. Request a new one.",
        )

    user.hashed_password = hash_password(payload.password)
    # Signing out everywhere is the point of a reset.
    for session in (
        await db.execute(select(RefreshSession).where(RefreshSession.user_id == user.id))
    ).scalars():
        if session.revoked_at is None:
            session.revoked_at = datetime.now(UTC)
    log.info("auth.password_reset", user_id=str(user.id))


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, db: DbSession
) -> None:
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Your current password is not correct."
        )
    user.hashed_password = hash_password(payload.new_password)
    db.add(user)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user