"""Fill the real Instacart cart from a small fixed list. Never checks out.

    python scripts/shop_demo.py            # Chrome visible; questions asked in this terminal
    python scripts/shop_demo.py --auto     # hidden Chrome; keeps existing cart, declines risky swaps
"""

import argparse
import asyncio
import sys
import time

sys.path.insert(0, "backend")

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import DayPlan, Ingredient, Meal, MealPlan, MealSpec  # noqa: E402
from app.nutrition.usda import USDAClient  # noqa: E402
from app.planner.llm import LLM  # noqa: E402
from app.shopping import consolidator, shopper  # noqa: E402
from app.shopping.instacart import InstacartSite  # noqa: E402
from app.shopping.session import BrowserSession, ensure_logged_in  # noqa: E402

FIVE_ITEMS = [("chicken_breast_boneless_skinless", 900), ("paneer", 300), ("toor_dal", 250),
              ("basmati_rice", 700), ("onion", 450)]
SAFE_ANSWERS = {"cart_clear_approval": "keep", "substitution_approval": "no"}


async def main(auto: bool) -> int:
    await db.init_db()
    spec = MealSpec(zip_code="61801", days=1, include_snacks=False, budget_weekly_usd=60,
                    macros={"calories": 2200, "protein_g": 140}, cuisines=["Indian"])
    plan = MealPlan(days=[DayPlan(day="Monday", meals=[Meal(
        slot="lunch", title="test list", prep_minutes=10,
        ingredients=[Ingredient(name=n, grams=g) for n, g in FIVE_ITEMS])])])
    grocery_list = consolidator.build(plan, spec)
    started = time.monotonic()

    async def emit(e):
        print(f"[{time.monotonic() - started:5.0f}s] {e['message']}")

    async def ask(kind, prompt, screenshot):
        if auto:
            print(f"  ? {prompt}\n  → auto-answer: {SAFE_ANSWERS[kind]}")
            return SAFE_ANSWERS[kind]
        return await asyncio.to_thread(input, f"\n{prompt}\n> ")

    async with BrowserSession(get_settings().single_user_id, headless=auto) as s:
        if not await ensure_logged_in(s.page):
            print("Not signed in. Run: python scripts/login.py")
            return 1
        report, complete = await shopper.fill_cart(
            spec, plan, grocery_list, site=InstacartSite(s.page), llm=LLM(),
            lookup=USDAClient().get_macros_per_100g, ask=ask, emit=emit)
        blocked = len(s.blocked)

    print(f"\n{'CART READY' if complete else 'NEEDS ATTENTION'} at {report.store} — "
          f"subtotal ${report.subtotal_usd:.2f}")
    for c in report.coverage:
        print(f"  {'✓' if c.covered else '✗'} {c.item:34} need {c.grams_needed:6.0f} g, in cart {c.grams_in_cart:6.0f} g")
    for line in report.lines:
        sub = f"  (instead of {line.substituted_for})" if line.substituted_for else ""
        print(f"    {line.product_name} ({line.pack_size}) × {line.packs} = ${line.line_total_usd:.2f}{sub}")
    for n in report.notes:
        print("  note:", n)
    print(f"  checkout guard blocked {blocked} checkout/payment request(s)")
    print(f"Took {time.monotonic() - started:.0f}s")
    return 0 if complete else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--auto", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args().auto)))
