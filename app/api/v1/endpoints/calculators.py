"""Calorie, BMI and cardio-burn calculators.

Open to visitors — they are a conversion tool on the landing page as well as a
portal feature. Signed-in clients get their saved stats pre-filled by the
frontend, and can push a result straight onto their profile targets.
"""

from fastapi import APIRouter, Request, Response, status

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.rate_limit import limiter
from app.schemas.tracking import (
    BmiRequest,
    BmiResponse,
    CalorieRequest,
    CalorieResponse,
    CardioBurnRequest,
    CardioBurnResponse,
)
from app.services import calculators

router = APIRouter(prefix="/calculators", tags=["calculators"])


@router.post("/calories", response_model=CalorieResponse)
@limiter.limit("60/minute")
async def calories(
    request: Request, response: Response, payload: CalorieRequest
) -> CalorieResponse:
    return calculators.calorie_plan(payload)


@router.post("/bmi", response_model=BmiResponse)
@limiter.limit("60/minute")
async def body_mass_index(
    request: Request, response: Response, payload: BmiRequest
) -> BmiResponse:
    return calculators.bmi(payload)


@router.post("/cardio-burn", response_model=CardioBurnResponse)
@limiter.limit("60/minute")
async def cardio(
    request: Request, response: Response, payload: CardioBurnRequest
) -> CardioBurnResponse:
    return calculators.cardio_burn(payload)


@router.post("/calories/apply", response_model=CalorieResponse, status_code=status.HTTP_200_OK)
async def apply_calorie_targets(
    payload: CalorieRequest, user: CurrentUser, db: DbSession
) -> CalorieResponse:
    """Save a calculated result as the signed-in client's daily targets."""
    result = calculators.calorie_plan(payload)
    profile = user.profile
    if profile is not None:
        profile.calorie_target = result.target_calories
        profile.protein_target_g = result.macros.protein_g
        profile.carb_target_g = result.macros.carbs_g
        profile.fat_target_g = result.macros.fat_g
        profile.activity_level = payload.activity_level
        profile.goal = payload.goal
        profile.current_weight_kg = payload.weight_kg
        profile.height_cm = payload.height_cm
        profile.sex = payload.sex
        db.add(profile)
    return result


@router.get("/reference")
async def reference() -> dict:
    """Static reference data so the frontend never hard-codes clinical numbers."""
    return {
        "bmi_bands": [
            {"label": "Underweight", "max": 18.5, "tone": "info"},
            {"label": "Healthy weight", "min": 18.5, "max": 24.9, "tone": "good"},
            {"label": "Overweight", "min": 25.0, "max": 29.9, "tone": "warn"},
            {"label": "Obese", "min": 30.0, "tone": "alert"},
        ],
        "activity_levels": [
            {"value": "sedentary", "label": "Sedentary — desk job, no training"},
            {"value": "light", "label": "Light — Level 1, 3 training days"},
            {"value": "moderate", "label": "Moderate — Level 2, 4 training days"},
            {"value": "active", "label": "Active — Level 3, 5–6 training days"},
            {"value": "very_active", "label": "Very active — training twice a day"},
        ],
        "disclaimer": (
            "These calculators give estimates for healthy adults. They are not medical "
            f"advice. Speak to your doctor before starting a new programme, or email "
            f"{settings.SUPPORT_EMAIL} to talk it through with Coach Auto."
        ),
    }
