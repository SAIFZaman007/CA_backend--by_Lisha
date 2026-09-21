"""
Automatic weekly meal plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import ActivityLevel, Goal, Sex, UnitSystem
from app.models.nutrition import Meal, MealItem, MealPlan
from app.models.user import ClientProfile
from app.schemas.tracking import CalorieRequest
from app.services.calculators import activity_level_for_days, calorie_plan

log = get_logger("nutrition.auto")

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"


class MealPlanInputError(ValueError):
    """The client's profile is missing something the plan needs."""


# =============================================================================
# Food table (per 100 g as eaten)
# =============================================================================


@dataclass(frozen=True, slots=True)
class Food:
    label: str
    kcal: float
    protein: float
    carbs: float
    fat: float
    min_g: int = 0
    max_g: int = 400
    step_g: int = 5
    # Foods counted in whole units ("3 eggs") rather than grams.
    unit_g: int | None = None
    unit_label: str | None = None


FOODS: dict[str, Food] = {
    # --- Protein ---------------------------------------------------------------
    "chicken": Food("grilled chicken breast", 165, 31.0, 0.0, 3.6, 90, 280),
    "turkey": Food("lean turkey breast", 135, 30.0, 0.0, 1.5, 90, 280),
    "beef": Food("lean beef mince (95%), cooked", 164, 26.0, 0.0, 6.5, 90, 250),
    "salmon": Food("baked salmon", 206, 22.0, 0.0, 12.0, 90, 220),
    "cod": Food("baked white fish (cod)", 105, 23.0, 0.0, 0.9, 100, 300),
    "tuna": Food("tuna in water, drained", 116, 26.0, 0.0, 0.8, 80, 200),
    "shrimp": Food("cooked shrimp", 99, 24.0, 0.2, 0.3, 90, 250),
    "tofu": Food("firm tofu", 144, 17.0, 3.0, 9.0, 100, 300),
    "eggs": Food("eggs", 143, 12.6, 0.7, 9.5, 50, 250, unit_g=50, unit_label="egg"),
    "egg_whites": Food("liquid egg whites", 52, 11.0, 0.7, 0.2, 60, 300),
    "greek_yogurt": Food("non-fat Greek yogurt", 59, 10.0, 3.6, 0.4, 100, 400),
    "cottage": Food("low-fat cottage cheese", 84, 11.0, 4.3, 2.3, 100, 350),
    "whey": Food("whey protein powder", 400, 80.0, 8.0, 6.0, 15, 60, step_g=5),
    # --- Carbohydrate -------------------------------------------------------------
    "oats": Food("rolled oats (dry)", 379, 13.2, 67.7, 6.5, 30, 130),
    "rice": Food("cooked white rice", 130, 2.7, 28.0, 0.3, 60, 450),
    "brown_rice": Food("cooked brown rice", 123, 2.7, 25.6, 1.0, 60, 450),
    "sweet_potato": Food("baked sweet potato", 90, 2.0, 20.7, 0.2, 80, 500),
    "potato": Food("baked potato", 93, 2.5, 21.0, 0.1, 80, 500),
    "quinoa": Food("cooked quinoa", 120, 4.4, 21.3, 1.9, 60, 400),
    "pasta": Food("cooked whole-wheat pasta", 149, 6.0, 30.0, 1.7, 60, 400),
    "bread": Food(
        "whole-grain bread", 247, 13.0, 41.0, 3.4, 40, 160, unit_g=40, unit_label="slice"
    ),
    "wrap": Food(
        "whole-wheat tortilla wrap", 297, 9.0, 49.0, 7.6, 60, 180, unit_g=60, unit_label="wrap"
    ),
    "rice_cakes": Food("rice cakes", 387, 8.0, 81.0, 2.8, 9, 72, unit_g=9, unit_label="rice cake"),
    "banana": Food("banana", 89, 1.1, 22.8, 0.3, 60, 240, step_g=10),
    "berries": Food("mixed berries", 57, 0.7, 14.5, 0.3, 60, 250, step_g=10),
    "apple": Food("apple", 52, 0.3, 13.8, 0.2, 100, 300, step_g=10),
    "black_beans": Food("black beans, cooked", 132, 8.9, 23.7, 0.5, 60, 300),
    "honey": Food("honey", 304, 0.3, 82.0, 0.0, 0, 30, step_g=5),
    # --- Fat ----------------------------------------------------------------------
    "olive_oil": Food("olive oil", 884, 0.0, 0.0, 100.0, 0, 30, step_g=5),
    "avocado": Food("avocado", 160, 2.0, 8.5, 14.7, 0, 150, step_g=10),
    "almonds": Food("almonds", 579, 21.0, 22.0, 50.0, 0, 50, step_g=5),
    "peanut_butter": Food("peanut butter", 588, 25.0, 20.0, 50.0, 0, 40, step_g=5),
    "walnuts": Food("walnuts", 654, 15.0, 14.0, 65.0, 0, 40, step_g=5),
    "cheese": Food("reduced-fat cheese", 280, 28.0, 3.0, 17.0, 0, 50, step_g=5),
    # --- Fixed extras -------------------------------------------------------------
    "broccoli": Food("steamed broccoli", 35, 2.4, 7.2, 0.4),
    "greens": Food("mixed salad greens", 17, 1.5, 3.0, 0.2),
    "spinach": Food("spinach", 23, 2.9, 3.6, 0.4),
    "veg_mix": Food("roasted mixed vegetables", 65, 2.6, 13.0, 0.2),
    "green_beans": Food("green beans", 35, 1.9, 7.9, 0.3),
    "salsa": Food("fresh salsa", 36, 1.5, 7.0, 0.2),
    "milk": Food("skim milk", 34, 3.4, 5.0, 0.1),
}


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    protein: str
    carb: str
    fat: str | None
    extras: tuple[tuple[str, int], ...] = ()


# Several options per slot; a day picks one of each, so a week rotates.
BREAKFASTS = (
    Template("Oats, whey & berries", "whey", "oats", "almonds", (("berries", 80),)),
    Template("Eggs on toast with spinach", "eggs", "bread", "avocado", (("spinach", 60),)),
    Template("Greek yogurt bowl", "greek_yogurt", "oats", "walnuts", (("berries", 100),)),
    Template("Egg-white scramble & potatoes", "egg_whites", "potato", "cheese", (("spinach", 60),)),
)
LUNCHES = (
    Template("Chicken, rice & greens", "chicken", "rice", "olive_oil", (("greens", 80),)),
    Template("Turkey & quinoa bowl", "turkey", "quinoa", "avocado", (("veg_mix", 120),)),
    Template("Tuna salad wrap", "tuna", "wrap", "olive_oil", (("greens", 60),)),
    Template("Beef & black bean bowl", "beef", "brown_rice", None, (("salsa", 60), ("greens", 60))),
    Template("Shrimp & sweet potato", "shrimp", "sweet_potato", "olive_oil", (("broccoli", 120),)),
)
DINNERS = (
    Template("Salmon, potatoes & greens", "salmon", "potato", None, (("green_beans", 120),)),
    Template("Lean beef & rice", "beef", "rice", None, (("broccoli", 150),)),
    Template("Chicken pasta & vegetables", "chicken", "pasta", "olive_oil", (("veg_mix", 120),)),
    Template("White fish & sweet potato", "cod", "sweet_potato", "olive_oil", (("broccoli", 150),)),
    Template("Tofu & brown rice stir-fry", "tofu", "brown_rice", "olive_oil", (("veg_mix", 150),)),
)
SNACKS = (
    Template("Protein shake & banana", "whey", "banana", "peanut_butter", (("milk", 250),)),
    Template("Cottage cheese & apple", "cottage", "apple", "almonds"),
    Template("Greek yogurt & rice cakes", "greek_yogurt", "rice_cakes", "peanut_butter"),
    Template("Tuna rice cakes", "tuna", "rice_cakes", "avocado"),
)


@dataclass(frozen=True, slots=True)
class Slot:
    label: str
    icon: str
    at: time
    options: tuple[Template, ...]


SLOTS_4 = (
    (Slot("Breakfast", "🍳", time(7, 30), BREAKFASTS), 0.25),
    (Slot("Lunch", "🥗", time(12, 30), LUNCHES), 0.32),
    (Slot("Afternoon snack", "🥤", time(16, 0), SNACKS), 0.13),
    (Slot("Dinner", "🍽️", time(19, 0), DINNERS), 0.30),
)
SLOTS_5 = (
    (Slot("Breakfast", "🍳", time(7, 30), BREAKFASTS), 0.22),
    (Slot("Lunch", "🥗", time(12, 30), LUNCHES), 0.28),
    (Slot("Afternoon snack", "🥤", time(16, 0), SNACKS), 0.12),
    (Slot("Dinner", "🍽️", time(19, 0), DINNERS), 0.26),
    (Slot("Evening snack", "🥛", time(21, 0), SNACKS), 0.12),
)
FIVE_MEAL_THRESHOLD = 2600


# =============================================================================
# Portion solving
# =============================================================================


@dataclass(slots=True)
class Portion:
    food: Food
    grams: int


@dataclass(slots=True)
class PlannedMeal:
    name: str
    icon: str
    at: time
    portions: list[Portion]

    def totals(self) -> tuple[int, int, int, int]:
        kcal = protein = carbs = fat = 0.0
        for portion in self.portions:
            factor = portion.grams / 100
            kcal += portion.food.kcal * factor
            protein += portion.food.protein * factor
            carbs += portion.food.carbs * factor
            fat += portion.food.fat * factor
        return round(kcal), round(protein), round(carbs), round(fat)


def _clamp(value: float, food: Food) -> float:
    return max(food.min_g, min(food.max_g, value))


def _round(grams: float, food: Food) -> int:
    if food.unit_g:
        units = max(1, round(grams / food.unit_g)) if grams >= food.unit_g / 2 else 0
        return max(units * food.unit_g, food.min_g if food.min_g else 0)
    step = food.step_g
    return int(max(food.min_g, round(grams / step) * step))


def _solve(template: Template, protein_t: float, carbs_t: float, fat_t: float) -> list[Portion]:
    """Gauss-Seidel over the three variable foods.

    Each food is dominant in its own macro, so a few sweeps converge; clamps
    keep every portion inside a realistic serving.
    """
    p_food = FOODS[template.protein]
    c_food = FOODS[template.carb]
    f_food = FOODS[template.fat] if template.fat else None
    extras = [(FOODS[key], grams) for key, grams in template.extras]

    base_p = sum(food.protein * g / 100 for food, g in extras)
    base_c = sum(food.carbs * g / 100 for food, g in extras)
    base_f = sum(food.fat * g / 100 for food, g in extras)

    gp, gc, gf = float(p_food.min_g or 100), float(c_food.min_g or 50), 0.0
    for _ in range(30):
        other_p = base_p + c_food.protein * gc / 100 + (f_food.protein * gf / 100 if f_food else 0)
        gp = _clamp((protein_t - other_p) * 100 / p_food.protein, p_food)

        other_c = base_c + p_food.carbs * gp / 100 + (f_food.carbs * gf / 100 if f_food else 0)
        gc = _clamp((carbs_t - other_c) * 100 / c_food.carbs, c_food)

        if f_food:
            other_f = base_f + p_food.fat * gp / 100 + c_food.fat * gc / 100
            gf = _clamp((fat_t - other_f) * 100 / f_food.fat, f_food)

    portions = [Portion(p_food, _round(gp, p_food)), Portion(c_food, _round(gc, c_food))]
    if f_food:
        rounded = _round(gf, f_food)
        if rounded > 0:
            portions.append(Portion(f_food, rounded))
    portions.extend(Portion(food, grams) for food, grams in extras)
    return [portion for portion in portions if portion.grams > 0]


def _label(portion: Portion, units: UnitSystem) -> str:
    food = portion.food
    if food.unit_g and food.unit_label:
        count = max(1, round(portion.grams / food.unit_g))
        noun = food.unit_label if count == 1 else f"{food.unit_label}s"
        suffix = (
            ""
            if food.label.endswith(f"{food.unit_label}s") or food.label == "eggs"
            else f" ({food.label})"
        )
        return f"{count} {noun}{suffix}"
    if food.label == "skim milk":
        return f"{portion.grams} ml {food.label}"
    if units == UnitSystem.IMPERIAL and portion.grams >= 30:
        ounces = portion.grams / 28.35
        return f"{portion.grams} g ({ounces:.1f} oz) {food.label}"
    return f"{portion.grams} g {food.label}"


# =============================================================================
# Targets
# =============================================================================


@dataclass(frozen=True, slots=True)
class Targets:
    calories: int
    protein_g: int
    carbs_g: int
    fat_g: int
    goal: Goal
    assumptions: tuple[str, ...]


def _age(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def targets_for(profile: ClientProfile, *, days_per_week: int | None = None) -> Targets:
    """Daily targets from the client's profile. Raises MealPlanInputError."""
    weight = profile.current_weight_kg or profile.starting_weight_kg
    if not profile.height_cm or not weight:
        raise MealPlanInputError(
            "Add height and current weight to the profile first — the plan is built from them."
        )

    assumptions: list[str] = []
    age = _age(profile.date_of_birth)
    if age is None or not 13 <= age <= 100:
        age = 30
        assumptions.append("age 30 (no date of birth on file)")

    sex = profile.sex
    if sex is None:
        # The lower of the two estimates: easier to add food than to undo a surplus.
        sex = Sex.FEMALE
        assumptions.append("the more conservative calorie estimate (sex not set)")

    activity = (
        activity_level_for_days(days_per_week)
        if days_per_week
        else (profile.activity_level or ActivityLevel.LIGHT)
    )

    result = calorie_plan(
        CalorieRequest(
            age=age,
            sex=sex,
            weight_kg=float(weight),
            height_cm=float(profile.height_cm),
            activity_level=activity,
            goal=profile.goal or Goal.MAINTAIN,
        )
    )
    return Targets(
        calories=result.target_calories,
        protein_g=result.macros.protein_g,
        carbs_g=result.macros.carbs_g,
        fat_g=result.macros.fat_g,
        goal=profile.goal or Goal.MAINTAIN,
        assumptions=tuple(assumptions),
    )


def build_week(targets: Targets, units: UnitSystem) -> dict[int, list[PlannedMeal]]:
    """Seven days of meals for these targets. Pure — no database."""
    slots = SLOTS_5 if targets.calories >= FIVE_MEAL_THRESHOLD else SLOTS_4
    week: dict[int, list[PlannedMeal]] = {}
    for day in range(7):
        meals: list[PlannedMeal] = []
        for slot_index, (slot, share) in enumerate(slots):
            # Different stride per slot so the same pairings do not repeat daily.
            template = slot.options[(day * (slot_index + 1) + slot_index) % len(slot.options)]
            portions = _solve(
                template,
                targets.protein_g * share,
                targets.carbs_g * share,
                targets.fat_g * share,
            )
            meals.append(PlannedMeal(template.name, slot.icon, slot.at, portions))
        week[day] = meals
    return week


# =============================================================================
# Persistence
# =============================================================================


async def _days_per_week(db: AsyncSession, client_id) -> int | None:
    from app.services.entitlements import active_subscription  # noqa: PLC0415 — avoids a cycle

    subscription = await active_subscription(db, client_id)
    if subscription and subscription.program:
        return subscription.program.days_per_week
    return None


async def generate_meal_plan(
    db: AsyncSession,
    client_id,
    *,
    assigned_by_id=None,
    activate: bool = True,
) -> MealPlan:
    """Build and store a new automatic plan. Raises MealPlanInputError."""
    profile = (
        (await db.execute(select(ClientProfile).where(ClientProfile.user_id == client_id)))
        .scalars()
        .first()
    )
    if profile is None:
        raise MealPlanInputError("This client has not filled in a profile yet.")

    targets = targets_for(profile, days_per_week=await _days_per_week(db, client_id))
    units = profile.unit_system or UnitSystem.IMPERIAL
    week = build_week(targets, units)

    notes = (
        "Built automatically from your height, weight, age, training days and goal. "
        "Swap like for like (chicken ↔ turkey ↔ fish, rice ↔ potatoes ↔ pasta) whenever "
        "you want, and weigh foods cooked unless marked dry. Your coach reviews and "
        "adjusts it at your check-ins."
    )
    if targets.assumptions:
        notes += (
            " Assumed "
            + "; ".join(targets.assumptions)
            + " — update your profile for a closer fit."
        )

    plan = MealPlan(
        client_id=client_id,
        assigned_by_id=assigned_by_id,
        name=f"Auto plan · {targets.goal.value.title()} · {targets.calories:,} kcal",
        phase=targets.goal,
        calorie_target=targets.calories,
        protein_target_g=targets.protein_g,
        carb_target_g=targets.carbs_g,
        fat_target_g=targets.fat_g,
        notes=notes,
        is_active=activate,
        source=SOURCE_AUTO,
    )
    db.add(plan)
    await db.flush()

    for day, meals in week.items():
        for order, planned in enumerate(meals):
            kcal, protein, carbs, fat = planned.totals()
            # Items attached through the relationship, not by foreign key, so
            # the collection is populated in the session and serialising the
            # plan never triggers a lazy load (MissingGreenlet under asyncio).
            db.add(
                Meal(
                    plan_id=plan.id,
                    day_of_week=day,
                    order_index=order,
                    name=planned.name,
                    serve_time=planned.at,
                    icon=planned.icon,
                    calories=kcal,
                    protein_g=protein,
                    carbs_g=carbs,
                    fat_g=fat,
                    items=[
                        MealItem(label=_label(portion, units)[:160], order_index=item_order)
                        for item_order, portion in enumerate(planned.portions)
                    ],
                )
            )

    if activate:
        await db.execute(
            update(MealPlan)
            .where(MealPlan.client_id == client_id, MealPlan.id != plan.id)
            .values(is_active=False)
        )

    # Fill empty profile targets so the dashboards show the same numbers.
    if profile.calorie_target is None:
        profile.calorie_target = targets.calories
        profile.protein_target_g = targets.protein_g
        profile.carb_target_g = targets.carbs_g
        profile.fat_target_g = targets.fat_g

    await db.flush()
    # Hand back a fully loaded plan (meals and their items) so every caller —
    # the portal, the dashboard, billing — can serialise it without further IO.
    plan = (
        await db.execute(
            select(MealPlan)
            .where(MealPlan.id == plan.id)
            .options(selectinload(MealPlan.meals).selectinload(Meal.items))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    log.info(
        "nutrition.auto_plan_generated",
        client_id=str(client_id),
        plan_id=str(plan.id),
        calories=targets.calories,
    )
    return plan


async def has_active_plan(db: AsyncSession, client_id) -> bool:
    return (
        await db.execute(
            select(MealPlan.id)
            .where(MealPlan.client_id == client_id, MealPlan.is_active.is_(True))
            .limit(1)
        )
    ).first() is not None


async def ensure_auto_meal_plan(db: AsyncSession, client_id) -> MealPlan | None:
    """
    Give a newly subscribed client a plan — if they have none yet.

    Never raises: this runs inside billing and profile flows, and a missing
    height must not fail a payment webhook. Runs in a SAVEPOINT so a failure
    here cannot poison the surrounding transaction either.
    """
    if not settings.AUTO_MEAL_PLAN_ENABLED:
        return None
    try:
        if await has_active_plan(db, client_id):
            return None
        async with db.begin_nested():
            return await generate_meal_plan(db, client_id)
    except MealPlanInputError as exc:
        log.info("nutrition.auto_plan_skipped", client_id=str(client_id), reason=str(exc))
    except Exception as exc:  # noqa: BLE001 — must never break the caller
        log.error("nutrition.auto_plan_failed", client_id=str(client_id), error=str(exc))
    return None