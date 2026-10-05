import pytest

from app import db
from app.models import GroceryItem, GroceryList, MealPlan, MealSpec
from app.shopping import shopper
from app.shopping.instacart import CartEntry
from fakes import FakeSite, ScriptedLLM, cand, fake_lookup, rough_day


def spec(**kw):
    return MealSpec(zip_code="12345", days=1, include_snacks=False, budget_weekly_usd=30,
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
        "substitute": [shopper.Picks(picks=[shopper.Pick(item="masoor_dal -> toor_dal @ quicklly-grocery",
                                                           index=sub_index)])],
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
    # Not at the first store, not at the second, and the substitute was declined.
    assert "Not found at Quicklly Indian Grocery, Meijer: masoor_dal" in report.notes


async def test_existing_cart_asks_before_clearing():
    old = CartEntry("Oreo Cookies", "13 oz", 4.99, 1, "ct")
    site = FakeSite(CATALOG, cart=[old])
    _, _, asked, events = await run(site, shopping_llm(), {"cart_clear_approval": "clear", "substitution_approval": "yes"})
    assert asked[0] == "cart_clear_approval" and "Cleared the existing Quicklly Indian Grocery cart" in events
    assert "Oreo Cookies" not in site.cart


async def test_kept_cart_items_are_reported_not_counted():
    site = FakeSite(CATALOG, cart=[CartEntry("Oreo Cookies", "13 oz", 4.99, 1, "ct")])
    report, _, _, _ = await run(site, shopping_llm(), {"cart_clear_approval": "keep", "substitution_approval": "yes"})
    assert "Also in your Quicklly Indian Grocery cart (not on the list): Oreo Cookies" in report.notes
    assert "Kept 1 item(s) already in your Quicklly Indian Grocery cart" in report.notes


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


async def test_warns_when_instacart_delivers_somewhere_else():
    site = FakeSite(CATALOG)
    site.delivery_zip = "99999"            # your account's address; the spec says 12345
    report, complete, _, events = await run(site, shopping_llm(), {"substitution_approval": "yes"})
    warning = ("Instacart is set to deliver to 99999, not your ZIP 12345. Stores and prices are for 99999. "
               "To shop for 12345, change the delivery address in Instacart and run again.")
    assert warning in report.notes and warning in events
    assert complete                         # a warning, not a blocker


async def test_no_warning_when_zip_matches_or_is_unknown():
    for seen in ("12345", None):
        site = FakeSite(CATALOG)
        site.delivery_zip = seen
        report, _, _, events = await run(site, shopping_llm(), {"substitution_approval": "yes"})
        assert not any("deliver to" in n for n in report.notes) and not any("deliver to" in e for e in events)


# ---- shopping across stores -------------------------------------------------------------------

SPLIT_CATALOG = {   # Quicklly has the chicken and paneer but no masoor dal; HMart has the dal.
    ("quicklly-grocery", "paneer"): [cand(0, "Nanak Paneer", "12 oz", 8.79, 340.2)],
    ("quicklly-grocery", "chicken breast"): [cand(0, "Halal Chicken Breast", "2 lb", 9.99, 907.2)],
    ("hmart", "masoor dal"): [cand(0, "Laxmi Masoor Dal", "2 lb", 4.49, 907.2)],
}


def split_llm():
    return ScriptedLLM({
        "store": [shopper.StoreShortlist(stores=["quicklly-grocery", "hmart"], hard_items=["paneer"], reason="")],
        "probe": [shopper.Picks(picks=[shopper.Pick(item="quicklly-grocery: paneer", index=0)])],
        "pick": [
            shopper.Picks(picks=[shopper.Pick(item="chicken_breast", index=0), shopper.Pick(item="paneer", index=0)]),
            shopper.Picks(picks=[shopper.Pick(item="masoor_dal", index=0)]),     # at HMart
        ],
    })


async def test_items_the_first_store_lacks_are_bought_at_the_next_store():
    site, llm = FakeSite(SPLIT_CATALOG), split_llm()
    report, complete, asked, events = await run(site, llm)

    assert complete and all(c.covered for c in report.coverage)
    assert site.adds_at == [("quicklly-grocery", "Halal Chicken Breast"), ("quicklly-grocery", "Nanak Paneer"),
                            ("hmart", "Laxmi Masoor Dal")]
    assert "Not found yet: masoor_dal. Looking at HMart" in events and "HMart has masoor_dal" in events
    assert "Added at HMart: Laxmi Masoor Dal (2 lb) × 1 — $4.49" in events
    assert report.store == "Quicklly Indian Grocery + HMart"
    assert report.stores == ["Quicklly Indian Grocery", "HMart"]
    assert report.subtotals == {"Quicklly Indian Grocery": 28.77, "HMart": 4.49}
    assert report.subtotal_usd == pytest.approx(33.26)
    assert {c.item: c.store for c in report.coverage} == {
        "chicken_breast": "Quicklly Indian Grocery", "paneer": "Quicklly Indian Grocery", "masoor_dal": "HMart"}
    assert [line.store for line in report.lines] == ["Quicklly Indian Grocery", "Quicklly Indian Grocery", "HMart"]
    assert report.notes[0].startswith("Your list is split across 2 stores (Quicklly Indian Grocery, HMart).")
    assert [p for p, _ in llm.calls] == ["store", "probe", "pick", "pick"]   # no substitute needed
    assert asked == []


async def test_a_store_with_everything_is_the_only_store():
    catalog = {**SPLIT_CATALOG, ("quicklly-grocery", "masoor dal"): [cand(0, "Rani Masoor Dal", "2 lb", 5.49, 907.2)]}
    llm = split_llm()
    llm.replies["pick"] = [shopper.Picks(picks=[shopper.Pick(item=i, index=0)
                                                for i in ("chicken_breast", "paneer", "masoor_dal")])]
    site = FakeSite(catalog)
    report, complete, _, events = await run(site, llm)
    assert complete and report.stores == ["Quicklly Indian Grocery"] and report.store == "Quicklly Indian Grocery"
    assert not any(e.startswith("Not found yet") for e in events)
    assert {s for s, _ in site.adds_at} == {"quicklly-grocery"}
    assert not any("split across" in n for n in report.notes)


async def test_never_more_stores_than_the_limit(monkeypatch):
    monkeypatch.setattr(shopper, "MAX_STORES", 1)
    site, llm = FakeSite(SPLIT_CATALOG), split_llm()
    report, complete, _, events = await run(site, llm)
    assert not complete
    assert not any("Looking at HMart" in e for e in events) and "hmart" not in {s for s, _ in site.adds_at}
    assert "Not found at Quicklly Indian Grocery: masoor_dal" in report.notes


async def test_second_store_cart_is_asked_about_before_it_is_used():
    old = CartEntry("Old Chips", "8 oz", 2.99, 1, "ct")
    site = FakeSite(SPLIT_CATALOG, cart=[old], cart_store="hmart")
    report, complete, asked, _ = await run(site, split_llm(), {"cart_clear_approval": "keep"})
    assert asked == ["cart_clear_approval"] and complete
    assert "Kept 1 item(s) already in your HMart cart" in report.notes
    assert "Also in your HMart cart (not on the list): Old Chips" in report.notes
    assert report.subtotals["HMart"] == pytest.approx(4.49 + 2.99)   # the kept item is in that cart's total
