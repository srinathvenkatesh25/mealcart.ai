import pytest

from app.models import MealPlan, MealSpec
from app.nutrition.solver import FAT_MAX_PER_MEAL_G, day_totals, solve_day, solve_plan
from app.nutrition.validator import check
from fakes import fake_lookup, meal, rough_day, PER_100G
from app.nutrition.usda import FoodMacros

MACROS = {n: FoodMacros(calories=k, protein_g=p, carbs_g=c, fat_g=f, fdc_id=0, description=n)
          for n, (k, p, c, f) in PER_100G.items()}


def spec(calories=2000, protein=150, **kw) -> MealSpec:
    return MealSpec(zip_code="61801", days=1, include_snacks=False,
                    macros={"calories": calories, "protein_g": protein}, **kw)


@pytest.mark.parametrize("calories, protein", [(2000, 150), (1800, 120), (2400, 160)])
async def test_solver_hits_targets_and_validator_agrees(calories, protein):
    s = spec(calories, protein)
    day, hit = await solve_day(rough_day(), s, fake_lookup)
    kcal, prot = day_totals(day, MACROS)
    assert hit, (kcal, prot)
    assert abs(kcal - calories) / calories <= 0.05 and abs(prot - protein) <= 10
    assert (await check(MealPlan(days=[day]), s, fake_lookup)).passed


async def test_solver_respects_bounds_and_keeps_spices():
    before = rough_day()
    after, _ = await solve_day(before, spec(), fake_lookup)
    for mb, ma in zip(before.meals, after.meals):
        fat = sum(i.grams for i in ma.ingredients if i.name == "vegetable_oil")
        assert fat <= FAT_MAX_PER_MEAL_G
        for ib, ia in zip(mb.ingredients, ma.ingredients):
            assert ib.name == ia.name
            if ib.grams <= 10:
                assert ia.grams == ib.grams  # spices untouched
            else:
                assert 0.5 * ib.grams - 5 <= ia.grams <= 2 * ib.grams + 5
                assert ia.grams % 5 == 0


async def test_solver_reports_unreachable_targets():
    # 400 g protein is not reachable within 2x portions.
    _, hit = await solve_day(rough_day(), spec(2000, 400), fake_lookup)
    assert not hit


async def test_unknown_ingredient_day_is_left_unchanged():
    day = rough_day(extra=[("dragon_fruit_paste", 50)])
    solved, hit = await solve_day(day, spec(), fake_lookup)
    assert solved == day and not hit


async def test_solve_plan_lists_missed_days():
    plan = MealPlan(days=[rough_day("Monday"), rough_day("Tuesday", extra=[("dragon_fruit_paste", 50)])])
    _, missed = await solve_plan(plan, spec(), fake_lookup)
    assert missed == ["Tuesday"]
