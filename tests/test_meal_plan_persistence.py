"""Automatic meal plans against the real database.

Guards the regression where a freshly generated plan could not be serialised
(`MissingGreenlet`): the admin and portal endpoints both return the plan they
just built, so every meal and item must already be loaded.
"""

from sqlalchemy import select

from app.api.v1.endpoints.admin.programming import _meal_plan_out
from app.core.database import SessionLocal
from app.models.enums import Goal, Sex
from app.models.user import ClientProfile, User
from app.schemas.tracking import MealPlanOut
from app.services.meal_planner import SOURCE_AUTO, generate_meal_plan


async def test_generated_plan_is_fully_loaded_and_serialisable(client, auth_headers):
    me = (await client.get("/api/v1/users/me", headers=auth_headers)).json()

    async with SessionLocal() as db:
        user = await db.get(User, me["id"])
        profile = (
            (await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id)))
            .scalars()
            .first()
        )
        if profile is None:
            profile = ClientProfile(user_id=user.id)
            db.add(profile)
        profile.height_cm = 178
        profile.current_weight_kg = 82
        profile.sex = Sex.MALE
        profile.goal = Goal.CUT
        await db.flush()

        plan = await generate_meal_plan(db, user.id)

        # Both response shapes — admin and portal — without any further IO.
        admin_out = _meal_plan_out(plan)
        portal_out = MealPlanOut.model_validate(plan)
        await db.rollback()

    assert admin_out.source == SOURCE_AUTO
    assert len({meal.day_of_week for meal in admin_out.meals}) == 7
    assert all(meal.items for meal in admin_out.meals)
    assert len(portal_out.meals) == len(admin_out.meals)