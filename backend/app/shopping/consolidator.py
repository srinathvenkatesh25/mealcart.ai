"""Meal plan → grocery list. Deterministic.

Sums each food's raw grams across the week, merges spelling variants, skips
pantry items and things nobody buys (water), attaches allowed substitutes,
and estimates price from earlier carts when it can.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from app.models import GroceryItem, GroceryList, MealPlan, MealSpec
from app.nutrition.maps import canonical_key, contains_term, exclusion_hits
from app.shopping.packs import packs_needed, target_qty

NEVER_BUY = ("water", "ice")
SUBSTITUTIONS_FILE = Path(__file__).with_name("substitutions.yaml")


@lru_cache
def substitution_table() -> dict[str, list[str]]:
    raw = yaml.safe_load(SUBSTITUTIONS_FILE.read_text()) or {}
    return {canonical_key(k): v for k, v in raw.items()}


def _excluded(name: str, spec: MealSpec) -> bool:
    return bool(exclusion_hits(name, spec.allergies, spec.dietary_restrictions, spec.dislikes))


def build(plan: MealPlan, spec: MealSpec,
          prices: dict[str, tuple[float, float]] | None = None) -> GroceryList:
    """`prices` maps canonical item → (pack_grams, unit_price_usd) from earlier carts."""
    totals: dict[str, float] = {}
    display: dict[str, str] = {}
    for day in plan.days:
        for meal in day.meals:
            for ing in meal.ingredients:
                # The validator already rejects excluded foods; this is the second lock.
                if _excluded(ing.name, spec):
                    raise ValueError(f"'{ing.name}' is excluded by the spec but reached the grocery list")
                key = canonical_key(ing.name)
                totals[key] = totals.get(key, 0.0) + ing.grams
                display.setdefault(key, ing.name)

    prices = prices or {}
    items, est_total, priced_all = [], 0.0, True
    for key, grams in sorted(totals.items()):
        name = display[key]
        if any(contains_term(key, w) for w in NEVER_BUY):
            continue
        if any(contains_term(key, p) or canonical_key(p) == key for p in spec.pantry):
            continue
        substitutes = [s for s in substitution_table().get(key, []) if not _excluded(s, spec)]
        est = None
        if key in prices:
            pack_g, unit_price = prices[key]
            est = round(unit_price * packs_needed(grams, pack_g), 2)
            est_total += est
        else:
            priced_all = False
        items.append(GroceryItem(name=name, total_grams=round(grams, 1),
                                 purchase_qty=target_qty(name, grams),
                                 substitutes=substitutes, est_price_usd=est))

    return GroceryList(items=items, est_total_usd=round(est_total, 2) if items and priced_all else None)
