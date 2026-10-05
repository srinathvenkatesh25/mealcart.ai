"""Fill the Instacart cart from a grocery list. Never checks out.

LLM calls (batched for free-tier quotas): 1 to shortlist stores, 1 to judge
which shortlisted store really stocks the hard-to-find items, 1 to pick
products for every item, and at most 1 more to pick substitutes. Everything
else (searching, pack counts, adding, verification) is deterministic.
"""

from typing import Protocol

from pydantic import BaseModel, Field

from app import db
from app.hitl import events
from app.hitl.events import Emit
from app.models import CartReport, GroceryItem, GroceryList, MealPlan, MealSpec
from app.nutrition import validator
from app.nutrition.maps import canonical_key
from app.nutrition.usda import MacroLookup, UnknownIngredientError
from app.planner.planner import StructuredLLM
from app.shopping.instacart import Candidate, CartEntry
from app.shopping.packs import packs_needed
from app.shopping.session import Ask
from app.shopping.verify import build_report

MAX_SHORTLIST = 3
MAX_PROBE_ITEMS = 4
MAX_SUBSTITUTES_TRIED = 2


class Site(Protocol):
    async def list_stores(self) -> list[tuple[str, str]]: ...
    async def search(self, store: str, query: str, item: str, limit: int = 8) -> list[Candidate]: ...
    async def add(self, store: str, query: str, product_name: str, packs: int) -> float: ...
    async def cart_lines(self, store: str) -> list[CartEntry]: ...
    async def clear_cart(self, store: str) -> None: ...


class StoreShortlist(BaseModel):
    stores: list[str] = Field(description="up to 3 store slugs, best first")
    hard_items: list[str] = Field(description="up to 4 grocery item names least likely to be stocked everywhere")
    reason: str


class Pick(BaseModel):
    item: str
    index: int | None = Field(description="candidate index, or null if no candidate is this food")
    reason: str = ""


class Picks(BaseModel):
    picks: list[Pick]


STORE_SYSTEM = """You choose which grocery store to shop at on Instacart. Reply only with JSON.
Pick general grocers or specialist grocers that fit the list (e.g. an Indian grocer for
Indian staples). Avoid membership warehouse clubs (bulk sizes), pharmacies, and non-food stores."""

PICK_SYSTEM = """You match grocery items to products in a store's search results. Reply only with JSON.
For each item choose the candidate that IS that food in a plain, ordinary form:
- brand and package size may differ; prefer store brands and better value;
- match the cut or type the item names: "breast" is not "thigh", "ground" is not "whole", "toor dal" is
  not "masoor dal". Prefer a product whose name states the cut. Take an unspecified one (e.g. just
  "boneless chicken") only when no candidate names it;
- never a ready meal, sauce, mix or snack made from it (e.g. "Palak Paneer" is not paneer);
- never an ingredient that merely appears alongside it in results;
- if no candidate is that food, use null.
Copy each `item` value exactly as given. Do not compute quantities; software does that."""


def search_query(item: str) -> str:
    return item.replace("_", " ")


def _render_candidates(rows: list[tuple[str, float, list[Candidate]]]) -> str:
    blocks = []
    for key, grams, cands in rows:
        opts = "\n".join(f"  [{c.index}] {c.name} — {c.size or '?'} — ${c.price}" for c in cands)
        need = f" (need about {grams:.0f} g)" if grams else ""
        blocks.append(f"item: {key}{need}\n{opts}")
    return "\n\n".join(blocks)


async def choose_store(spec: MealSpec, grocery_list: GroceryList, site: Site, llm: StructuredLLM,
                       emit: Emit, run_id: str | None) -> tuple[str, str]:
    stores = await site.list_stores()
    names = dict(stores)
    shortlist = await llm.structured(
        StoreShortlist, STORE_SYSTEM,
        "Stores near the customer (slug: name):\n" + "\n".join(f"{s}: {n}" for s, n in stores)
        + "\n\nGrocery list:\n" + "\n".join(i.name for i in grocery_list.items),
        purpose="store", run_id=run_id, temperature=0,
    )
    slugs = [s for s in shortlist.stores if s in names][:MAX_SHORTLIST] or [stores[0][0]]
    hard = [i for i in shortlist.hard_items if any(g.name == i for g in grocery_list.items)][:MAX_PROBE_ITEMS]
    hard = hard or [i.name for i in grocery_list.items[:MAX_PROBE_ITEMS]]

    # Probe: search each shortlisted store for the hard items, then one LLM call judges
    # every result set with the same "plain form of this food" rule as product picks.
    # (A word match can't: "Deep Spinach Paneer" contains "paneer" but isn't paneer.)
    rows, keys = [], {}
    for slug in slugs:
        for item in hard:
            cands = await site.search(slug, search_query(item), item)
            if cands:
                key = f"{slug}: {item}"
                rows.append((key, 0.0, cands))
                keys[key] = (slug, item)
    judged = await _pick(llm, rows, "probe", run_id)
    stocked = {slug: [item for k, (s, item) in keys.items() if s == slug and judged.get(k) is not None]
               for slug in slugs}

    best, best_score = slugs[0], -1
    for slug in slugs:  # LLM order breaks ties
        found = stocked[slug]
        await emit(events.progress(run_id or "", "shop", f"{names[slug]} stocks {len(found)}/{len(hard)} "
                                                         f"hard-to-find items ({', '.join(found) or 'none'})"))
        if len(found) > best_score:
            best, best_score = slug, len(found)
    return best, names[best]


async def _substitution_ok(spec: MealSpec, plan: MealPlan, original: str, substitute: str,
                           lookup: MacroLookup) -> tuple[bool, str]:
    """Re-validate the week with the swap applied; the substitute's macros come from USDA."""
    key = canonical_key(original)
    swapped = plan.model_copy(deep=True)
    for day in swapped.days:
        for meal in day.meals:
            for ing in meal.ingredients:
                if canonical_key(ing.name) == key:
                    ing.name = substitute
    try:
        result = await validator.check(swapped, spec, lookup)
    except UnknownIngredientError:
        return False, f"no nutrition data for {substitute}"
    return result.passed, "; ".join(result.deltas[:3])


async def fill_cart(spec: MealSpec, plan: MealPlan, grocery_list: GroceryList, *, site: Site,
                    llm: StructuredLLM, lookup: MacroLookup, ask: Ask, emit: Emit,
                    run_id: str | None = None) -> tuple[CartReport, bool]:
    """Returns the verified report and whether every item is covered (cart_ready)."""
    rid = run_id or ""
    store, store_name = await choose_store(spec, grocery_list, site, llm, emit, run_id)
    await emit(events.progress(rid, "shop", f"Shopping at {store_name}"))
    notes: list[str] = []

    existing = await site.cart_lines(store)
    cleared = False
    if existing:
        answer = await ask("cart_clear_approval",
                           f"Your {store_name} cart already has {len(existing)} item(s): "
                           f"{', '.join(e.name for e in existing[:5])}. Clear it first? (clear/keep)", None)
        if answer.strip().lower().startswith("clear"):
            await site.clear_cart(store)
            cleared = True
            await emit(events.progress(rid, "shop", "Cleared the existing cart"))
        else:
            notes.append(f"Kept {len(existing)} item(s) already in the cart")

    # 1. Search every item, then one LLM call picks for all of them. A product already in a
    #    kept cart is reused for its item, so a re-run never adds a second, different product.
    in_cart = {e.name for e in existing} if existing and not cleared else set()
    found = [(item, await site.search(store, search_query(item.name), item.name))
             for item in grocery_list.items]
    reused = {i.name: next(c.index for c in cands if c.name in in_cart)
              for i, cands in found if any(c.name in in_cart for c in cands)}
    asked = await _pick(llm, [(i.name, i.total_grams, c) for i, c in found if c and i.name not in reused],
                        "pick", run_id)
    picks = asked | reused  # what's already in the cart wins, even if the LLM answered for it

    chosen: dict[str, tuple[Candidate, str, str | None]] = {}  # item → (candidate, query, substituted_for)
    missing: list[GroceryItem] = []
    for item, cands in found:
        idx = picks.get(item.name)
        if idx is not None and 0 <= idx < len(cands) and cands[idx].in_stock:
            chosen[item.name] = (cands[idx], search_query(item.name), None)
        else:
            missing.append(item)

    # 2. Substitutes for what's missing: search, one LLM call, then re-validate macros.
    sub_rows = []
    for item in missing:
        for sub in item.substitutes[:MAX_SUBSTITUTES_TRIED]:
            cands = await site.search(store, search_query(sub), sub)
            if cands:
                sub_rows.append((item, sub, cands))
    if sub_rows:
        # The LLM judges each candidate against the substitute food, e.g. "masoor_dal".
        sub_picks = await _pick(llm, [(f"{i.name} -> {s}", i.total_grams, c) for i, s, c in sub_rows],
                                "substitute", run_id)
        for item, sub, cands in sub_rows:
            idx = sub_picks.get(f"{item.name} -> {sub}")
            if item.name in chosen or idx is None or not (0 <= idx < len(cands)) or not cands[idx].in_stock:
                continue
            ok, why = await _substitution_ok(spec, plan, item.name, sub, lookup)
            if not ok:
                answer = await ask("substitution_approval",
                                   f"{store_name} has no {item.name}. Use {cands[idx].name} ({sub}) instead? "
                                   f"It breaks the plan's checks: {why}. (yes/no)", None)
                if not answer.strip().lower().startswith("y"):
                    continue
            chosen[item.name] = (cands[idx], search_query(sub), item.name)
            notes.append(f"Substituted {sub} for {item.name}: {cands[idx].name}")
    for item in grocery_list.items:
        if item.name not in chosen:
            notes.append(f"Unavailable at {store_name}: {item.name}")

    # 3. Add to cart; pack counts are Python arithmetic.
    for item in grocery_list.items:
        if item.name not in chosen:
            continue
        cand, query, _ = chosen[item.name]
        packs = packs_needed(item.total_grams, cand.pack_grams) if cand.pack_grams else 1
        if not cand.pack_grams:
            notes.append(f"Could not read the size of {cand.name}; added 1, check the amount")
        try:
            qty = await site.add(store, query, cand.name, packs)
        except LookupError as e:
            notes.append(str(e))
            continue
        price = f" — ${cand.price * qty:.2f}" if cand.price else ""
        verb = "Already in cart" if cand.name in in_cart else "Added"
        await emit(events.progress(rid, "shop", f"{verb}: {cand.name} ({cand.size}) × {qty:g}{price}"))
        if cand.price and cand.pack_grams:
            await db.record_price(spec.user_id, canonical_key(item.name), cand.name, cand.pack_grams, cand.price)

    # 4. Verify against the grocery list, from the cart as Instacart shows it.
    cart = await site.cart_lines(store)
    picked = {name: (c.name, c.pack_grams or 0.0, sub) for name, (c, _, sub) in chosen.items()}
    return build_report(store_name, grocery_list, cart, picked, spec.budget_weekly_usd, notes)


async def _pick(llm: StructuredLLM, rows, purpose: str, run_id: str | None) -> dict[str, int | None]:
    if not rows:
        return {}
    reply = await llm.structured(Picks, PICK_SYSTEM, _render_candidates(rows), purpose=purpose,
                                 run_id=run_id, temperature=0)
    return {p.item: p.index for p in reply.picks}
