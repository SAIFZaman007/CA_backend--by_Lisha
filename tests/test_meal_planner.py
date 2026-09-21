"""The automatic meal plan generator — pure maths, no database."""

from datetime import date

import pytest

from app.models.enums import Goal, Sex, UnitSystem
from app.models.user import ClientProfile
from app.services.meal_planner import (
    FIVE_MEAL_THRESHOLD,
    MealPlanInputError,
    Targets,
    build_week,
    targets_for,
)


def _day_totals(meals):
    totals = [0, 0, 0, 0]
    for meal in meals:
        totals = [a + b for a, b in zip(totals, meal.totals(), strict=True)]
    return totals


@pytest.mark.parametrize(
    ("calories", "protein", "carbs", "fat"),
    [(1500, 130, 140, 42), (2200, 160, 230, 65), (3200, 180, 380, 90)],
)
def test_week_lands_close_to_macro_targets(calories, protein, carbs, fat):
    week = build_week(Targets(calories, protein, carbs, fat, Goal.CUT, ()), UnitSystem.METRIC)

    assert sorted(week) == list(range(7))
    for meals in week.values():
        expected_meals = 5 if calories >= FIVE_MEAL_THRESHOLD else 4
        assert len(meals) == expected_meals
        _, p, _c, _f = _day_totals(meals)
        # Protein is the macro that matters most for body composition.
        assert abs(p - protein) / protein < 0.12
        for meal in meals:
            assert meal.portions, "every meal lists what to eat"
            assert all(portion.grams > 0 for portion in meal.portions)


def test_week_is_varied():
    week = build_week(Targets(2200, 160, 230, 65, Goal.MAINTAIN, ()), UnitSystem.METRIC)
    lunches = {week[day][1].name for day in range(7)}
    assert len(lunches) >= 3


def test_targets_need_height_and_weight():
    with pytest.raises(MealPlanInputError):
        targets_for(ClientProfile(goal=Goal.CUT))


def test_targets_follow_goal():
    base = {
        "sex": Sex.MALE,
        "height_cm": 180,
        "current_weight_kg": 85,
        "date_of_birth": date(1994, 1, 1),
        "unit_system": UnitSystem.METRIC,
    }
    cut = targets_for(ClientProfile(goal=Goal.CUT, **base), days_per_week=4)
    build = targets_for(ClientProfile(goal=Goal.BUILD, **base), days_per_week=4)
    assert cut.calories < build.calories
    assert not cut.assumptions


def test_missing_sex_and_age_are_assumed_and_reported():
    profile = ClientProfile(goal=Goal.MAINTAIN, height_cm=165, current_weight_kg=62)
    targets = targets_for(profile)
    assert len(targets.assumptions) == 2