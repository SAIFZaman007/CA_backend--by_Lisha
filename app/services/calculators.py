"""Fitness maths. Kept server-side so the website, the client portal and the
coach dashboard always agree on the numbers.

References
- BMR: Mifflin-St Jeor (1990), the formula with the lowest average error for
  the general population.
- MET values: 2024 Compendium of Physical Activities, rounded to one decimal.
"""

from app.models.enums import ActivityLevel, CardioType, Goal, Intensity, Sex
from app.schemas.tracking import (
    BmiRequest,
    BmiResponse,
    CalorieRequest,
    CalorieResponse,
    CardioBurnRequest,
    CardioBurnResponse,
    MacroSplit,
)

ACTIVITY_FACTORS: dict[ActivityLevel, float] = {
    ActivityLevel.SEDENTARY: 1.2,
    ActivityLevel.LIGHT: 1.375,  # Level 1 — 3 training days
    ActivityLevel.MODERATE: 1.55,  # Level 2 — 4 training days
    ActivityLevel.ACTIVE: 1.725,  # Level 3 — 5–6 training days
    ActivityLevel.VERY_ACTIVE: 1.9,
}

# Applied to TDEE. A 20% deficit and a 10% surplus are conservative, sustainable
# rates that protect lean mass.
GOAL_FACTORS: dict[Goal, float] = {Goal.CUT: 0.80, Goal.MAINTAIN: 1.0, Goal.BUILD: 1.10}

# Protein g per kg bodyweight, fat as a share of total calories.
GOAL_MACROS: dict[Goal, tuple[float, float]] = {
    Goal.CUT: (2.2, 0.25),
    Goal.MAINTAIN: (1.8, 0.28),
    Goal.BUILD: (2.0, 0.25),
}

BASE_METS: dict[CardioType, float] = {
    CardioType.WALKING: 3.5,
    CardioType.RUNNING: 9.8,
    CardioType.CYCLING: 7.5,
    CardioType.ROWING: 7.0,
    CardioType.ELLIPTICAL: 5.0,
    CardioType.STAIR_CLIMBER: 9.0,
    CardioType.SWIMMING: 8.0,
    CardioType.HIIT: 8.0,
    CardioType.SPORTS: 6.5,
    CardioType.OTHER: 5.0,
}

INTENSITY_MULTIPLIERS: dict[Intensity, float] = {
    Intensity.LOW: 0.75,
    Intensity.MODERATE: 1.0,
    Intensity.HIGH: 1.3,
}

MIN_SAFE_CALORIES: dict[Sex, int] = {Sex.FEMALE: 1200, Sex.MALE: 1500}


def calculate_bmr(sex: Sex, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor resting metabolic rate in kcal/day."""
    base = (10 * weight_kg) + (6.25 * height_cm) - (5 * age)
    return base + 5 if sex == Sex.MALE else base - 161


def calculate_macros(target_calories: int, weight_kg: float, goal: Goal) -> MacroSplit:
    protein_per_kg, fat_ratio = GOAL_MACROS[goal]
    protein_g = round(weight_kg * protein_per_kg)
    fat_g = round((target_calories * fat_ratio) / 9)
    remaining = target_calories - (protein_g * 4) - (fat_g * 9)
    carbs_g = max(round(remaining / 4), 0)
    return MacroSplit(protein_g=protein_g, carbs_g=carbs_g, fat_g=fat_g)


def calorie_plan(payload: CalorieRequest) -> CalorieResponse:
    bmr = calculate_bmr(payload.sex, payload.weight_kg, payload.height_cm, payload.age)
    tdee = bmr * ACTIVITY_FACTORS[payload.activity_level]
    target = tdee * GOAL_FACTORS[payload.goal]

    floor = MIN_SAFE_CALORIES[payload.sex]
    note = (
        "Targets are a starting point. Track for two weeks, then adjust with your coach "
        "based on what the scale and the mirror actually do."
    )
    if target < floor:
        target = floor
        note = (
            f"Your target was raised to the {floor} kcal floor. Eating below this for long "
            "stretches costs muscle and energy — talk to your coach before going lower."
        )

    target_calories = int(round(target / 10) * 10)
    return CalorieResponse(
        bmr=round(bmr),
        tdee=round(tdee),
        target_calories=target_calories,
        macros=calculate_macros(target_calories, payload.weight_kg, payload.goal),
        note=note,
    )


def bmi(payload: BmiRequest) -> BmiResponse:
    height_m = payload.height_cm / 100
    value = round(payload.weight_kg / (height_m**2), 1)

    if value < 18.5:
        category = "Underweight"
    elif value < 25:
        category = "Healthy weight"
    elif value < 30:
        category = "Overweight"
    else:
        category = "Obese"

    healthy_range = (round(18.5 * height_m**2, 1), round(24.9 * height_m**2, 1))
    return BmiResponse(
        bmi=value,
        category=category,
        healthy_weight_range_kg=healthy_range,
        note=(
            "BMI is a screening number, not a diagnosis. It cannot tell muscle from fat, so "
            "trained lifters often read high. Use tape measurements and photos alongside it."
        ),
    )


def cardio_burn(payload: CardioBurnRequest) -> CardioBurnResponse:
    met = round(BASE_METS[payload.activity_type] * INTENSITY_MULTIPLIERS[payload.intensity], 1)
    # kcal = MET x 3.5 x kg / 200 per minute
    calories = round(met * 3.5 * payload.weight_kg / 200 * payload.duration_minutes)
    return CardioBurnResponse(
        calories_burned=calories,
        met_value=met,
        note=(
            "An estimate from average energy cost. A chest-strap or wrist heart-rate monitor "
            "will give you a closer number for your own body."
        ),
    )


def activity_level_for_days(days_per_week: int) -> ActivityLevel:
    if days_per_week <= 2:
        return ActivityLevel.SEDENTARY
    if days_per_week == 3:
        return ActivityLevel.LIGHT
    if days_per_week == 4:
        return ActivityLevel.MODERATE
    return ActivityLevel.ACTIVE


def kg_to_lb(kg: float) -> float:
    return round(kg * 2.2046226218, 1)


def lb_to_kg(lb: float) -> float:
    return round(lb / 2.2046226218, 2)


def cm_to_in(cm: float) -> float:
    return round(cm / 2.54, 1)


def in_to_cm(inches: float) -> float:
    return round(inches * 2.54, 1)
