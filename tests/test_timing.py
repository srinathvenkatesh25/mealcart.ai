import pytest

from app.models import DayPlan, MealPlan, MealSpec
from app.nutrition.timing import apply_minimums, minimum_minutes
from app.nutrition import validator
from fakes import fake_lookup, meal


def spec(**kw) -> MealSpec:
    return MealSpec(zip_code="61801", days=1, include_snacks=False,
                    macros={"calories": 2000, "protein_g": 150}, **kw)


def floor(ingredients, equipment=("stove",)):
    return minimum_minutes(meal("lunch", ingredients, prep_minutes=5), list(equipment))


@pytest.mark.parametrize(
    "ingredients, equipment, expected",
    [
        ([("chicken_breast_boneless_skinless", 130), ("basmati_rice", 80)], ("stove",), (15, "chicken_breast_boneless_skinless")),
        ([("basmati_rice", 100)], ("stove",), (15, "basmati_rice")),
        ([("brown_rice", 100)], ("stove",), (30, "brown_rice")),
        ([("brown_rice", 100)], ("pressure_cooker",), (20, "brown_rice")),
        ([("masoor_dal", 80)], ("stove",), (20, "masoor_dal")),
        ([("masoor_dal", 80)], ("stove", "pressure_cooker"), (12, "masoor_dal")),
        ([("chickpeas_dry", 110)], ("stove",), (50, "chickpeas_dry")),   # "Roasted Spiced Chickpeas", 2 min
        ([("chickpeas_dry", 110)], ("pressure_cooker",), (25, "chickpeas_dry")),
        ([("ground_chicken", 130)], ("stove",), (10, "ground_chicken")),
        ([("potato", 200)], ("stove",), (15, "potato")),
        ([("lamb_shoulder", 200)], ("stove",), (20, "lamb_shoulder")),
    ],
)
def test_slowest_ingredient_sets_the_floor(ingredients, equipment, expected):
    assert floor(ingredients, equipment) == expected


@pytest.mark.parametrize("ingredient", [
    "rice_flour", "rice_noodles", "chickpeas_canned", "chickpea_flour", "besan", "cooked_chicken",
    "chicken_stock", "paneer", "eggs", "spinach", "greek_yogurt_nonfat", "ground_beef", "potato_chips",
])
def test_quick_or_not_raw_foods_have_no_floor(ingredient):
    assert floor([(ingredient, 100)])[0] == 0


def test_raises_never_lowers_and_explains_in_notes():
    plan = MealPlan(days=[DayPlan(day="Monday", meals=[
        meal("lunch", [("chicken_breast_boneless_skinless", 130), ("basmati_rice", 80)], prep_minutes=5,
             title="One Pot Chicken Pulao"),
        meal("dinner", [("masoor_dal", 80)], prep_minutes=25),     # already longer than the floor
        meal("breakfast", [("eggs", 100)], prep_minutes=5),         # nothing slow
    ])])
    fixed, changed = apply_minimums(plan, spec())
    lunch, dinner, breakfast = fixed.days[0].meals
    assert changed == 1
    assert (lunch.prep_minutes, dinner.prep_minutes, breakfast.prep_minutes) == (15, 25, 5)
    assert lunch.notes == "Time raised to 15 min: chicken breast boneless skinless needs at least that long."
    assert plan.days[0].meals[0].prep_minutes == 5      # the input plan is not mutated


async def test_a_meal_that_cannot_fit_your_limit_is_flagged_by_the_validator():
    plan = MealPlan(days=[DayPlan(day="Monday", meals=[
        meal("evening", [("chickpeas_dry", 110)], prep_minutes=2, title="Roasted Spiced Chickpeas"),
    ])])
    fixed, _ = apply_minimums(plan, spec())
    result = await validator.check(fixed, spec(max_prep_minutes=30),
                                   lambda n: fake_lookup("masoor_dal"))
    hint = [d for d in result.deltas if "takes" in d]
    assert hint == ["Monday evening ('Roasted Spiced Chickpeas'): takes 50 min, limit is 30 — simplify or swap it"]
