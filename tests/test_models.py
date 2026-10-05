import pytest
from pydantic import ValidationError

from app.models import MacroTargets, Meal, MealSpec


def test_mealspec_defaults_apply():
    spec = MealSpec(zip_code="61801", macros=MacroTargets(calories=2500, protein_g=140))
    assert spec.days == 7
    assert spec.max_prep_minutes == 30
    assert spec.effort_level == "low_moderate"
    assert spec.cooking_skill == "basic"
    assert spec.equipment == ["stove", "microwave", "oven", "rice_cooker"]
    assert spec.cuisines == [] and spec.pantry == [] and spec.dietary_restrictions == []


def test_mealspec_round_trip_with_new_fields():
    spec = MealSpec(
        zip_code="61801",
        macros={"calories": 2200, "protein_g": 150},
        cuisines=["Indian", "Mexican"],
        dislikes=["mushrooms", "mayonnaise"],
        dietary_restrictions=["no_beef"],
        max_prep_minutes=25,
        effort_level="low",
        cooking_skill="intermediate",
        equipment=["stove", "rice_cooker"],
        pantry=["salt", "turmeric"],
        budget_weekly_usd=60,
    )
    assert MealSpec.model_validate_json(spec.model_dump_json()) == spec


def test_macro_targets_have_no_carbs_or_fat():
    assert set(MacroTargets.model_fields) == {"calories", "protein_g"}


@pytest.mark.parametrize(
    "bad",
    [
        {"effort_level": "extreme"},
        {"cooking_skill": "chef"},
        {"days": 0},
        {"days": 8},
        {"max_prep_minutes": 0},
    ],
)
def test_mealspec_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        MealSpec(zip_code="61801", macros={"calories": 2000, "protein_g": 120}, **bad)


def test_meal_requires_prep_minutes_and_valid_slot():
    with pytest.raises(ValidationError):
        Meal(slot="brunch", title="x", ingredients=[], prep_minutes=10)
    with pytest.raises(ValidationError):
        Meal(slot="lunch", title="x", ingredients=[])
