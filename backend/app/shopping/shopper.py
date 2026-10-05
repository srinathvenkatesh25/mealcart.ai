"""Fill Instacart carts from a grocery list. Never checks out.

The best store gets everything it stocks. Items it lacks are looked for at
the next-best shortlisted stores, up to MAX_STORES in total (each store is a
separate cart with its own fee and minimum). Only items no store has fall
back to substitutes.

LLM calls (batched for free-tier quotas): 1 to shortlist stores, 1 to judge
which shortlisted store really stocks the hard-to-find items, 1 to pick
products per store used, and at most 1 more to pick substitutes. Everything
else (searching, pack counts, adding, verification) is deterministic.
"""

from dataclasses import dataclass, field
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

MAX_SHORTLIST = 4
MAX_STORES = 3          # most stores one list is split across; each has its own cart, fee and minimum
MAX_PROBE_ITEMS = 4
MAX_SUBSTITUTES_TRIED = 2


class Site(Protocol):
    async def list_stores(self) -> list[tuple[str, str]]: ...
    async def search(self, store: str, query: str, item: str, limit: int = 8) -> list[Candidate]: ...
    async def add(self, store: str, query: str, product_name: str, packs: int) -> float: ...
    async def cart_lines(self, store: str) -> list[CartEntry]: ...
    async def clear_cart(self, store: str) -> None: ...
    delivery_zip: str | None


@dataclass
class Store:
    slug: str
    name: str
    in_cart: set[str] = field(default_factory=set)   # products in a kept cart: reused, never re-picked


@dataclass
class Assignment:
    store: Store
    cand: Candidate
    query: str
    substituted_for: str | None = None


class StoreShortlist(BaseModel):
    stores: list[str] = Field(description="up to 4 store slugs, best first")
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


async def choose_stores(spec: MealSpec, grocery_list: GroceryList, site: Site, llm: StructuredLLM,
                        emit: Emit, run_id: str | None) -> list[Store]:
    """Shortlisted stores, best first: by how many hard-to-find items they really stock."""
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
    for slug in slugs:
        found = stocked[slug]
        await emit(events.progress(run_id or "", "shop", f"{names[slug]} stocks {len(found)}/{len(hard)} "
                                                         f"hard-to-find items ({', '.join(found) or 'none'})"))
    ranked = sorted(slugs, key=lambda s: -len(stocked[s]))   # stable: the LLM's order breaks ties
    return [Store(s, names[s]) for s in ranked]


async def _open_store(store: Store, first: bool, *, site: Site, spec: MealSpec, ask: Ask, emit: Emit,
                      rid: str, notes: list[str]) -> None:
    """Look at a store's cart before using it, and ask before emptying it."""
    existing = await site.cart_lines(store.slug)
    if first and site.delivery_zip and site.delivery_zip != spec.zip_code:
        warning = (f"Instacart is set to deliver to {site.delivery_zip}, not your ZIP {spec.zip_code}. Stores and "
                   f"prices are for {site.delivery_zip}. To shop for {spec.zip_code}, change the delivery address "
                   "in Instacart and run again.")
        notes.append(warning)
        await emit(events.progress(rid, "shop", warning))
    if not existing:
        return
    answer = await ask("cart_clear_approval",
                       f"Your {store.name} cart already has {len(existing)} item(s): "
                       f"{', '.join(e.name for e in existing[:5])}. Clear it first? (clear/keep)", None)
    if answer.strip().lower().startswith("clear"):
        await site.clear_cart(store.slug)
        await emit(events.progress(rid, "shop", f"Cleared the existing {store.name} cart"))
    else:
        store.in_cart = {e.name for e in existing}
        notes.append(f"Kept {len(existing)} item(s) already in your {store.name} cart")


async def _match_at(store: Store, items: list[GroceryItem], *, site: Site, llm: StructuredLLM,
                    run_id: str | None) -> tuple[dict[str, Assignment], list[GroceryItem]]:
    """Search one store for these items; one LLM call picks for all of them.

    A product already in a kept cart is reused for its item, so a re-run never adds a
    second, different product.
    """
    found = [(item, await site.search(store.slug, search_query(item.name), item.name)) for item in items]
    reused = {i.name: next(c.index for c in cands if c.name in store.in_cart)
              for i, cands in found if any(c.name in store.in_cart for c in cands)}
    asked = await _pick(llm, [(i.name, i.total_grams, c) for i, c in found if c and i.name not in reused],
                        "pick", run_id)
    picks = asked | reused  # what's already in the cart wins, even if the LLM answered for it
    got: dict[str, Assignment] = {}
    missing: list[GroceryItem] = []
    for item, cands in found:
        idx = picks.get(item.name)
        if idx is not None and 0 <= idx < len(cands) and cands[idx].in_stock:
            got[item.name] = Assignment(store, cands[idx], search_query(item.name))
        else:
            missing.append(item)
    return got, missing


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
    notes: list[str] = []
    ranked = await choose_stores(spec, grocery_list, site, llm, emit, run_id)

    # 1. The best store first; whatever it lacks goes to the next store, up to MAX_STORES.
    assigned: dict[str, Assignment] = {}
    opened: list[Store] = []
    remaining = list(grocery_list.items)
    for n, store in enumerate(ranked):
        if not remaining or len({a.store.slug for a in assigned.values()}) >= MAX_STORES:
            break
        if n == 0:
            await emit(events.progress(rid, "shop", f"Shopping at {store.name}"))
        else:
            await emit(events.progress(rid, "shop", f"Not found yet: {', '.join(i.name for i in remaining)}. "
                                                    f"Looking at {store.name}"))
        await _open_store(store, n == 0, site=site, spec=spec, ask=ask, emit=emit, rid=rid, notes=notes)
        opened.append(store)
        got, remaining = await _match_at(store, remaining, site=site, llm=llm, run_id=run_id)
        assigned |= got
        if n > 0 and got:
            await emit(events.progress(rid, "shop", f"{store.name} has {', '.join(got)}"))

    # 2. Substitutes only for what no store had: search, one LLM call, then re-validate macros.
    sub_rows = []
    for item in remaining:
        for sub in item.substitutes[:MAX_SUBSTITUTES_TRIED]:
            for store in opened:
                cands = await site.search(store.slug, search_query(sub), sub)
                if cands:
                    sub_rows.append((item, sub, store, cands))
    if sub_rows:
        # The LLM judges each candidate against the substitute food, e.g. "masoor_dal".
        sub_picks = await _pick(llm, [(f"{i.name} -> {s} @ {st.slug}", i.total_grams, c)
                                      for i, s, st, c in sub_rows], "substitute", run_id)
        for item, sub, store, cands in sub_rows:
            idx = sub_picks.get(f"{item.name} -> {sub} @ {store.slug}")
            if item.name in assigned or idx is None or not (0 <= idx < len(cands)) or not cands[idx].in_stock:
                continue
            ok, why = await _substitution_ok(spec, plan, item.name, sub, lookup)
            if not ok:
                answer = await ask("substitution_approval",
                                   f"No store has {item.name}. Use {cands[idx].name} ({sub}) from {store.name} "
                                   f"instead? It breaks the plan's checks: {why}. (yes/no)", None)
                if not answer.strip().lower().startswith("y"):
                    continue
            assigned[item.name] = Assignment(store, cands[idx], search_query(sub), item.name)
            notes.append(f"Substituted {sub} for {item.name}: {cands[idx].name} ({store.name})")
    searched = ", ".join(s.name for s in opened)
    for item in grocery_list.items:
        if item.name not in assigned:
            notes.append(f"Not found at {searched}: {item.name}")

    used = [s for s in opened if any(a.store is s for a in assigned.values())]
    if len(used) > 1:
        notes.insert(0, f"Your list is split across {len(used)} stores ({', '.join(s.name for s in used)}). "
                        "Each is a separate Instacart cart with its own delivery fee and minimum order.")

    # 3. Add to cart; pack counts are Python arithmetic.
    for item in grocery_list.items:
        a = assigned.get(item.name)
        if a is None:
            continue
        cand = a.cand
        packs = packs_needed(item.total_grams, cand.pack_grams) if cand.pack_grams else 1
        if not cand.pack_grams:
            notes.append(f"Could not read the size of {cand.name}; added 1, check the amount")
        try:
            qty = await site.add(a.store.slug, a.query, cand.name, packs)
        except LookupError as e:
            notes.append(str(e))
            continue
        price = f" — ${cand.price * qty:.2f}" if cand.price else ""
        verb = "Already in cart" if cand.name in a.store.in_cart else "Added"
        where = f" at {a.store.name}" if len(used) > 1 else ""
        await emit(events.progress(rid, "shop", f"{verb}{where}: {cand.name} ({cand.size}) × {qty:g}{price}"))
        if cand.price and cand.pack_grams:
            await db.record_price(spec.user_id, canonical_key(item.name), cand.name, cand.pack_grams, cand.price)

    # 4. Verify against the grocery list, from each cart as Instacart shows it.
    carts = {s.name: await site.cart_lines(s.slug) for s in (used or opened[:1])}
    picked = {name: (a.store.name, a.cand.name, a.cand.pack_grams or 0.0, a.substituted_for)
              for name, a in assigned.items()}
    return build_report(grocery_list, carts, picked, spec.budget_weekly_usd, notes)


async def _pick(llm: StructuredLLM, rows, purpose: str, run_id: str | None) -> dict[str, int | None]:
    if not rows:
        return {}
    reply = await llm.structured(Picks, PICK_SYSTEM, _render_candidates(rows), purpose=purpose,
                                 run_id=run_id, temperature=0)
    return {p.item: p.index for p in reply.picks}
