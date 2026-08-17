"""The signed-in client's own account and intake profile."""

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.models.user import ClientProfile, User
from app.schemas.user import ClientProfileOut, ClientProfileUpdate, UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def read_me(user: CurrentUser) -> User:
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(payload: UserUpdate, user: CurrentUser, db: DbSession) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.add(user)
    await db.flush()
    return user


@router.get("/me/profile", response_model=ClientProfileOut)
async def read_profile(user: CurrentUser, db: DbSession) -> ClientProfile:
    if user.profile is None:
        profile = ClientProfile(user_id=user.id)
        db.add(profile)
        await db.flush()
        await db.refresh(user, ["profile"])
        return profile
    return user.profile


@router.patch("/me/profile", response_model=ClientProfileOut)
async def update_profile(
    payload: ClientProfileUpdate, user: CurrentUser, db: DbSession
) -> ClientProfile:
    """Intake and preferences. Height, weight and sex feed the calculators;
    sleep and cardio targets drive the wellness rings."""
    profile = user.profile
    if profile is None:
        profile = ClientProfile(user_id=user.id)
        db.add(profile)
        await db.flush()

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(profile, field, value)

    if profile.starting_weight_kg is None and profile.current_weight_kg is not None:
        profile.starting_weight_kg = profile.current_weight_kg

    # Intake counts as complete once we know enough to write a programme.
    if all(
        getattr(profile, field) is not None
        for field in ("sex", "height_cm", "current_weight_kg", "date_of_birth")
    ):
        profile.onboarding_completed = True

    db.add(profile)
    await db.flush()
    return profile
