"""Reusable FastAPI dependencies: current user, role gates, pagination."""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.enums import UserRole
from app.models.user import User

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