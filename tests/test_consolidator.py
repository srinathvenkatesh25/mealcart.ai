import pytest

from app import db
from app.models import MealPlan, MealSpec
from app.shopping.consolidator import build
from fakes import meal, rough_day
from app.models import DayPlan


def spec(**kw) -> MealSpec:
    return MealSpec(zip_code="61801", macros={"calories": 2000, "protein_g": 150}, **kw)


def week_plan(days=7) -> MealPlan:
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][:days]
    return MealPlan(days=[rough_day(n) for n in names])


def by_name(grocery_list):
    return {i.name: i for i in grocery_list.items}


def test_week_collapses_to_deduplicated_totals():
    gl = build(week_plan(), spec())
    items = by_name(gl)
    # rough_day: rice 100 + 80 per day; oil 5 + 10 + 10; onion 50 + 50
    assert items["basmati_rice"].total_grams == 7 * 180
    assert items["vegetable_oil"].total_grams == 7 * 25
    assert items["onion"].total_grams == 7 * 100
    assert len(gl.items) == len(items) == 7
    assert items["turmeric_ground"].purchase_qty == "1 small pack"
    assert items["eggs"].purchase_qty == "14 eggs"


def test_spelling_variants_merge():
    plan = MealPlan(days=[DayPlan(day="Monday", meals=[
        meal("breakfast", [("eggs", 100)]), meal("lunch", [("egg", 50)]), meal("dinner", [("Eggs", 50)]),
    ])])
    gl = build(plan, spec())
    assert [(i.name, i.total_grams) for i in gl.items] == [("eggs", 200)]


def test_pantry_and_water_are_skipped():
    plan = MealPlan(days=[rough_day(extra=[("water", 500), ("sea_salt", 3)])])
    names = set(by_name(build(plan, spec(pantry=["salt", "Turmeric Ground", "vegetable oil"]))))
    assert names == {"eggs", "onion", "chicken_breast", "basmati_rice", "masoor_dal"}


def test_substitutes_attached_and_filtered_by_exclusions():
    plan = MealPlan(days=[DayPlan(day="Monday", meals=[meal("lunch", [("paneer", 200), ("toor_dal", 80)])])])
    items = by_name(build(plan, spec(allergies=["soy"])))
    assert items["paneer"].substitutes == ["halloumi"]           # tofu dropped: soy
    assert items["toor_dal"].substitutes == ["masoor_dal", "yellow_split_peas", "moong_dal"]


def test_excluded_ingredient_is_a_hard_error():
    plan = MealPlan(days=[rough_day(extra=[("button_mushrooms", 50)])])
    with pytest.raises(ValueError, match="button_mushrooms"):
        build(plan, spec(dislikes=["mushrooms"]))


def test_price_estimate_from_history():
    prices = {"chicken_breast": (680.4, 6.99)}
    items = by_name(build(week_plan(1), spec(), prices))
    assert items["chicken_breast"].est_price_usd == 6.99      # 200 g fits one 1.5 lb pack
    assert items["onion"].est_price_usd is None
    assert build(week_plan(1), spec(), prices).est_total_usd is None  # not every item priced


async def test_latest_prices_reads_most_recent():
    await db.init_db()
    await db.record_price("user_default", "chicken_breast", "Store Chicken", 680.4, 6.99)
    await db.record_price("user_default", "chicken_breast", "Store Chicken", 680.4, 7.49)
    assert await db.latest_prices("user_default") == {"chicken_breast": (680.4, 7.49)}
