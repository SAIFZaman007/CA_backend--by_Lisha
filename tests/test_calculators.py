"""The maths has to be right — clients set their intake by these numbers."""

import pytest

from app.models.enums import ActivityLevel, Goal, Sex
from app.schemas.tracking import BmiRequest, CalorieRequest
from app.services import calculators


def test_bmr_matches_mifflin_st_jeor_by_hand():
    # 10(70) + 6.25(165) - 5(38) - 161 = 700 + 1031.25 - 190 - 161
    assert calculators.calculate_bmr(Sex.FEMALE, 70, 165, 38) == pytest.approx(1380.25)
    assert calculators.calculate_bmr(Sex.MALE, 80, 180, 30) == pytest.approx(1780.0)


def test_cut_target_is_twenty_percent_below_maintenance():
    result = calculators.calorie_plan(
        CalorieRequest(
            age=38, sex=Sex.FEMALE, weight_kg=70, height_cm=165,
            activity_level=ActivityLevel.LIGHT, goal=Goal.CUT,
        )
    )
    assert result.target_calories == pytest.approx(result.tdee * 0.8, abs=10)


def test_calorie_floor_protects_against_crash_dieting():
    """A very small woman on a cut must never be told to eat under 1200 kcal."""
    result = calculators.calorie_plan(
        CalorieRequest(
            age=65, sex=Sex.FEMALE, weight_kg=42, height_cm=145,
            activity_level=ActivityLevel.SEDENTARY, goal=Goal.CUT,
        )
    )
    assert result.target_calories >= 1200
    assert "floor" in result.note


def test_macros_add_up_to_the_calorie_target():
    result = calculators.calorie_plan(
        CalorieRequest(
            age=30, sex=Sex.MALE, weight_kg=85, height_cm=180,
            activity_level=ActivityLevel.MODERATE, goal=Goal.BUILD,
        )
    )
    m = result.macros
    assert m.protein_g * 4 + m.carbs_g * 4 + m.fat_g * 9 == pytest.approx(
        result.target_calories, abs=15
    )


def test_bmi_matches_the_reference_case():
    result = calculators.bmi(BmiRequest(weight_kg=66, height_cm=162.56))
    assert result.bmi == 25.0
    assert result.category == "Overweight"
