import pytest

from app import db
from app.models import GroceryItem, GroceryList, MealPlan, MealSpec
from app.shopping import shopper
from app.shopping.instacart import CartEntry
from fakes import FakeSite, ScriptedLLM, cand, fake_lookup, rough_day


def spec(**kw):
    return MealSpec(zip_code="61801", days=1, include_snacks=False, budget_weekly_usd=30,
                    macros={"calories": 2000, "protein_g": 150}, **kw)


GROCERIES = GroceryList(items=[
    GroceryItem(name="chicken_breast", total_grams=1400, purchase_qty="≈ 3.25 lb"),
    GroceryItem(name="paneer", total_grams=300, purchase_qty="≈ 11 oz"),
    GroceryItem(name="masoor_dal", total_grams=80, purchase_qty="≈ 3 oz", substitutes=["toor_dal"]),
])

CATALOG = {
    ("meijer", "paneer"): [cand(0, "Deep Spinach Paneer", "10 oz", 6.99, 283)],
    ("quicklly-grocery", "paneer"): [cand(0, "Deep Paneer Tikka Masala", "9 oz", 5.12, 255),
                                     cand(1, "Nanak Paneer", "12 oz", 8.79, 340.2)],
    ("quicklly-grocery", "chicken breast"): [cand(0, "Halal Chicken Breast", "2 lb", 9.99, 907.2)],
    ("quicklly-grocery", "masoor dal"): [cand(0, "Deep Masoor Dal", "4 lb", 7.99, 1814.4, in_stock=False)],
    ("quicklly-grocery", "toor dal"): [cand(0, "Rani Toor Dal", "32 oz", 5.99, 907.2)],
}


async def run(site, llm, ask_answers=None, plan=None):
    await db.init_db()
    asked, events = [], []

    async def ask(kind, prompt, shot):
        asked.append(kind)
        return (ask_answers or {})[kind]

    async def emit(e):
        events.append(e["message"])

    report, complete = await shopper.fill_cart(
        spec(), plan or MealPlan(days=[rough_day()]), GROCERIES, site=site, llm=llm,
        lookup=fake_lookup, ask=ask, emit=emit)
    return report, complete, asked, events


def shopping_llm(sub_index=0):
    return ScriptedLLM({
        "store": [shopper.StoreShortlist(stores=["meijer", "quicklly-grocery"], hard_items=["paneer"], reason="")],
        "probe": [shopper.Picks(picks=[
            shopper.Pick(item="meijer: paneer", index=None),           # spinach paneer is a ready meal
            shopper.Pick(item="quicklly-grocery: paneer", index=1),
        ])],
        "pick": [shopper.Picks(picks=[
            shopper.Pick(item="chicken_breast", index=0),
            shopper.Pick(item="paneer", index=1),       # not the tikka masala
            shopper.Pick(item="masoor_dal", index=0),   # out of stock -> substitute
        ])],
        "substitute": [shopper.Picks(picks=[shopper.Pick(item="masoor_dal -> toor_dal", index=sub_index)])],
    })


async def test_store_choice_follows_probe_and_cart_is_filled_and_verified():
    site = FakeSite(CATALOG)
    llm = shopping_llm()
    # Toor dal for masoor dal would change the plan's macros; the fake lookup has no toor_dal,
    # so the swap can't be auto-validated and you're asked.
    report, complete, asked, events = await run(site, llm, {"substitution_approval": "yes"})

    assert report.store == "Quicklly Indian Grocery"          # Meijer only has a ready meal
    assert "Meijer stocks 0/1" in events[0] and "Quicklly Indian Grocery stocks 1/1" in events[1]
    assert site.adds == [("Halal Chicken Breast", 2), ("Nanak Paneer", 1), ("Rani Toor Dal", 1)]
    assert complete and all(c.covered for c in report.coverage)
    assert asked == ["substitution_approval"]
    assert [l.substituted_for for l in report.lines] == [None, None, "masoor_dal"]
    assert report.subtotal_usd == pytest.approx(9.99 * 2 + 8.79 + 5.99)
    assert report.over_budget_by_usd == pytest.approx(4.76)
    assert [p for p, _ in llm.calls] == ["store", "probe", "pick", "substitute"]


async def test_declined_substitution_leaves_item_uncovered():
    report, complete, _, _ = await run(FakeSite(CATALOG), shopping_llm(), {"substitution_approval": "no"})
    assert not complete
    assert [c.item for c in report.coverage if not c.covered] == ["masoor_dal"]
    assert "Unavailable at Quicklly Indian Grocery: masoor_dal" in report.notes


async def test_existing_cart_asks_before_clearing():
    old = CartEntry("Oreo Cookies", "13 oz", 4.99, 1, "ct")
    site = FakeSite(CATALOG, cart=[old])
    _, _, asked, events = await run(site, shopping_llm(), {"cart_clear_approval": "clear", "substitution_approval": "yes"})
    assert asked[0] == "cart_clear_approval" and "Cleared the existing cart" in events
    assert "Oreo Cookies" not in site.cart


async def test_kept_cart_items_are_reported_not_counted():
    site = FakeSite(CATALOG, cart=[CartEntry("Oreo Cookies", "13 oz", 4.99, 1, "ct")])
    report, _, _, _ = await run(site, shopping_llm(), {"cart_clear_approval": "keep", "substitution_approval": "yes"})
    assert "Also in cart (not on the list): Oreo Cookies" in report.notes
    assert "Kept 1 item(s) already in the cart" in report.notes


async def test_rerun_does_not_double_add():
    site = FakeSite(CATALOG)
    answers = {"substitution_approval": "yes", "cart_clear_approval": "keep"}
    await run(site, shopping_llm(), answers)
    # Second run: the LLM would now pick the tikka masala for paneer, but the paneer
    # already in the kept cart wins and the LLM isn't asked about it.
    llm = shopping_llm()
    llm.replies["pick"] = [shopper.Picks(picks=[shopper.Pick(item="paneer", index=0)])]
    report, complete, _, events = await run(site, llm, answers)
    assert "Already in cart: Nanak Paneer (12 oz) × 1 — $8.79" in events
    pick_prompt = next(u for p, u in llm.calls if p == "pick")
    assert "item: paneer" not in pick_prompt and "item: chicken_breast" not in pick_prompt
    assert complete and {e.name: e.quantity for e in site.cart.values()} == {
        "Halal Chicken Breast": 2, "Nanak Paneer": 1, "Rani Toor Dal": 1}


def test_pick_prompt_asks_for_the_named_cut():
    assert '"breast" is not "thigh"' in shopper.PICK_SYSTEM
    assert "only when no candidate names it" in shopper.PICK_SYSTEM
