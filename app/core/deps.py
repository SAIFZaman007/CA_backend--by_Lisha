"""Reusable FastAPI dependencies: current user, role gates, paid-feature gates."""

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.enums import UserRole
from app.models.user import User
from app.services.entitlements import Entitlement, entitlement_for

bearer_scheme = HTTPBearer(auto_error=False, description="Bearer access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Your session has expired. Sign in again to continue.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise CREDENTIALS_ERROR

    payload = decode_token(credentials.credentials, "access")
    if payload is None:
        raise CREDENTIALS_ERROR

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise CREDENTIALS_ERROR from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_coach(user: CurrentUser) -> User:
    if user.role not in (UserRole.COACH, UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This area is for coach accounts only.",
        )
    return user


CurrentCoach = Annotated[User, Depends(get_current_coach)]


async def get_optional_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User | None:
    if credentials is None:
        return None
    try:
        return await get_current_user(db, credentials)
    except HTTPException:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def get_current_admin(user: CurrentUser) -> User:
    """Strictly `admin`. Used for the destructive corners of the dashboard —
    deleting accounts, changing someone's role, removing a pricing plan.

    A `coach` can run the day-to-day: read records, write programmes, reply to
    messages. Only an `admin` can change who has access to what.
    """
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That action needs an admin account.",
        )
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


# --- Paid features ------------------------------------------------------------

UPGRADE_MESSAGE = "Upgrade to a suitable program to unlock direct coaching and personalized guidance."


async def get_entitlement(db: DbSession, user: CurrentUser) -> Entitlement:
    """What the signed-in account has paid for, resolved once per request."""
    return await entitlement_for(db, user)


CurrentEntitlement = Annotated[Entitlement, Depends(get_entitlement)]


def require_feature(feature: str) -> Callable[..., Coroutine[Any, Any, Entitlement]]:
    """
    A dependency that admits only accounts entitled to `feature`.

    Attached once per router in `api.v1.router`, so a new endpoint added to a
    paid area is covered the moment it is written — the failure mode of
    per-endpoint guards is the endpoint somebody forgets.

    402 Payment Required (not 403) is deliberate: it separates "you have not
    bought this" from "you are not allowed this", which is what lets the portal
    answer a 402 with the upgrade panel and a 403 with an error. The body names
    the feature and where to buy it, so the client never hard-codes copy.
    """

    async def guard(entitlement: CurrentEntitlement) -> Entitlement:
        if not entitlement.has(feature):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "subscription_required",
                    "feature": feature,
                    "message": UPGRADE_MESSAGE,
                    "upgrade_path": "/portal/billing",
                },
            )
        return entitlement

    guard.__name__ = f"require_{feature}"
    return guard