import pytest

from app.models import DayPlan, Ingredient, Meal, MealPlan, MealSpec
from app.nutrition import validator
from app.nutrition.usda import FoodMacros, UnknownIngredientError

PER_100G = {
    "chicken_breast": (120, 22.5, 0.0, 2.6),
    "basmati_rice": (365, 7.1, 80.0, 0.7),
    "masoor_dal": (352, 24.6, 63.1, 1.1),
    "vegetable_oil": (884, 0.0, 0.0, 100.0),
    "button_mushrooms": (22, 3.1, 3.3, 0.3),
}


async def fake_lookup(name: str) -> FoodMacros:
    if name not in PER_100G:
        raise UnknownIngredientError(name)
    kcal, p, c, f = PER_100G[name]
    return FoodMacros(calories=kcal, protein_g=p, carbs_g=c, fat_g=f, fdc_id=0, description=name)


def spec(**overrides) -> MealSpec:
    base = dict(zip_code="12345", days=1, macros={"calories": 2000, "protein_g": 155}, include_snacks=True)
    return MealSpec(**{**base, **overrides})


def meal(slot, ingredients, **kw) -> Meal:
    defaults = dict(title=f"{slot} meal", prep_minutes=20, equipment_used=["stove"], steps=["cook"])
    return Meal(slot=slot, ingredients=[Ingredient(name=n, grams=g) for n, g in ingredients], **{**defaults, **kw})


def day(name="Monday", chicken=500, rice=250, extra_meals=(), **lunch_kw) -> DayPlan:
    # 500 chicken + 250 rice + 100 dal + 15 oil = 1997.1 kcal, 154.85 g protein
    return DayPlan(day=name, meals=[
        meal("lunch", [("chicken_breast", chicken), ("basmati_rice", rice), ("vegetable_oil", 15)], **lunch_kw),
        meal("dinner", [("masoor_dal", 100)]),
        *extra_meals,
    ])


async def test_exact_pass():
    result = await validator.check(MealPlan(days=[day()]), spec(), fake_lookup)
    assert result.passed, result.deltas
    monday = result.per_day["Monday"]
    assert monday["calories"] == pytest.approx(1997.1)
    assert monday["protein_g"] == pytest.approx(154.85, abs=0.1)


async def test_carbs_and_fat_are_not_gated():
    # 15 g oil swapped for 0 changes fat massively but calories stay within 5%.
    plan = MealPlan(days=[day()])
    plan.days[0].meals[0].ingredients[2].grams = 5  # -88 kcal, fat drops ~10 g
    result = await validator.check(plan, spec(), fake_lookup)
    assert result.passed, result.deltas
    assert "fat_g" in result.per_day["Monday"]


async def test_single_day_calorie_failure_and_delta_math():
    plan = MealPlan(days=[day("Monday"), day("Tuesday", rice=305)])
    result = await validator.check(plan, spec(days=2), fake_lookup)
    assert not result.passed
    assert len(result.deltas) == 1
    hint = result.deltas[0]
    # 305 g rice adds 200.75 kcal -> 197.85 over; 197.85 / 3.65 = 54 -> ~50 g
    assert hint.startswith("Tuesday: calories 198kcal over target")
    assert "basmati_rice is the largest contributor" in hint and "reduce it by ~50g" in hint


async def test_protein_under_hint_names_protein_source():
    result = await validator.check(MealPlan(days=[day(chicken=400)]), spec(), fake_lookup)
    protein = [d for d in result.deltas if "protein" in d]
    # 22.65 g short / 0.225 g per g = 100.7 -> ~100 g
    assert protein == ["Monday: protein 23g under target (chicken_breast is the largest contributor) — increase it by ~100g"]


async def test_unknown_ingredient_becomes_hint():
    plan = MealPlan(days=[day(extra_meals=[meal("breakfast", [("dragon_fruit_paste", 50)])])])
    result = await validator.check(plan, spec(), fake_lookup)
    assert not result.passed
    assert any("'dragon_fruit_paste' has no USDA entry" in d for d in result.deltas)
    assert not any("calories" in d for d in result.deltas)  # no macro hints on incomplete days


async def test_dislike_allergy_and_restriction():
    plan = MealPlan(days=[day(extra_meals=[meal("breakfast", [("button_mushrooms", 10)])])])
    result = await validator.check(
        plan, spec(dislikes=["Mushrooms"], allergies=["mushroom"], dietary_restrictions=["vegetarian"]), fake_lookup
    )
    joined = "\n".join(result.deltas)
    assert "violates dislike 'Mushrooms'" in joined
    assert "violates allergy 'mushroom'" in joined
    assert "'chicken_breast' violates restriction 'vegetarian'" in joined


async def test_prep_time_equipment_and_steps():
    plan = MealPlan(days=[day(prep_minutes=45, equipment_used=["stove", "air fryer", "knife"], steps=["s"] * 9)])
    result = await validator.check(plan, spec(), fake_lookup)
    joined = "\n".join(result.deltas)
    assert "takes 45 min, limit is 30" in joined
    assert "needs air_fryer" in joined and "knife" not in joined.split("needs")[1].split("which")[0]
    assert "9 steps is too many for a basic cook" in joined


async def test_steps_limit_only_for_basic_skill():
    plan = MealPlan(days=[day(steps=["s"] * 12)])
    assert (await validator.check(plan, spec(cooking_skill="intermediate"), fake_lookup)).passed


async def test_snacks_off_rejects_snack_slots():
    snack = meal("evening", [("button_mushrooms", 10)])
    result = await validator.check(MealPlan(days=[day(extra_meals=[snack])]), spec(include_snacks=False), fake_lookup)
    assert any("snacks are turned off" in d for d in result.deltas)


async def test_wrong_day_count():
    result = await validator.check(MealPlan(days=[day()]), spec(days=2), fake_lookup)
    assert "plan has 1 days, expected 2" in result.deltas


async def test_same_meal_at_most_twice_a_week():
    days = [day(name) for name in ["Monday", "Tuesday", "Wednesday"]]
    result = await validator.check(MealPlan(days=days), spec(days=3), fake_lookup)
    variety = [d for d in result.deltas if "already appears" in d]
    # lunch and dinner each repeat a third time on Wednesday
    assert variety == [
        "Wednesday lunch: 'lunch meal' already appears on Monday, Tuesday — choose a different meal",
        "Wednesday dinner: 'dinner meal' already appears on Monday, Tuesday — choose a different meal",
    ]
