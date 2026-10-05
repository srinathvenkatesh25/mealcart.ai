"""Shared test doubles: an offline USDA table and a scripted LLM."""

from app.models import DayPlan, Ingredient, Meal, MealPlan
from app.nutrition.usda import FoodMacros, UnknownIngredientError
from app.shopping.instacart import Candidate, CartEntry

PER_100G = {
    "chicken_breast": (120, 22.5, 0.0, 2.6),
    "basmati_rice": (365, 7.1, 80.0, 0.7),
    "masoor_dal": (352, 24.6, 63.1, 1.1),
    "vegetable_oil": (884, 0.0, 0.0, 100.0),
    "button_mushrooms": (22, 3.1, 3.3, 0.3),
    "onion": (40, 1.1, 9.3, 0.1),
    "turmeric_ground": (312, 9.7, 67.1, 3.3),
    "eggs": (143, 12.6, 0.7, 9.5),
}


async def fake_lookup(name: str) -> FoodMacros:
    if name not in PER_100G:
        raise UnknownIngredientError(name)
    kcal, p, c, f = PER_100G[name]
    return FoodMacros(calories=kcal, protein_g=p, carbs_g=c, fat_g=f, fdc_id=0, description=name)


def meal(slot, ingredients, **kw) -> Meal:
    defaults = dict(title=f"{slot} meal", prep_minutes=20, equipment_used=["stove"], steps=["cook"])
    return Meal(slot=slot, ingredients=[Ingredient(name=n, grams=g) for n, g in ingredients], **{**defaults, **kw})


def rough_day(name="Monday", extra=()) -> DayPlan:
    """A sensible day whose portions are deliberately off target."""
    return DayPlan(day=name, meals=[
        meal("breakfast", [("eggs", 100), ("onion", 50), ("vegetable_oil", 5)]),
        meal("lunch", [("chicken_breast", 200), ("basmati_rice", 100), ("vegetable_oil", 10),
                       ("turmeric_ground", 2), *extra]),
        meal("dinner", [("masoor_dal", 80), ("basmati_rice", 80), ("onion", 50), ("vegetable_oil", 10)]),
    ])


def week(*days: DayPlan) -> MealPlan:
    return MealPlan(days=list(days))


class ScriptedLLM:
    """Returns queued replies per purpose; records every call."""

    def __init__(self, replies: dict[str, list]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, str]] = []

    async def structured(self, schema, system, user, *, purpose, run_id=None, temperature=0.3):
        self.calls.append((purpose, user))
        reply = self.replies[purpose].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def cand(i, name, size, price, grams, in_stock=True):
    return Candidate(index=i, name=name, size=size, price=price, pack_grams=grams, unit="ct", in_stock=in_stock)


class FakeSite:
    """Stores whose search results and carts live in memory; one cart per store, like Instacart."""

    def __init__(self, catalog: dict[tuple[str, str], list[Candidate]], cart: list[CartEntry] | None = None,
                 cart_store: str = "quicklly-grocery"):
        self.catalog = catalog
        self.carts: dict[str, dict[str, CartEntry]] = {}
        if cart:
            self.carts[cart_store] = {e.name: e for e in cart}
        self.prices = {c.name: c for cs in catalog.values() for c in cs}
        self.adds: list[tuple[str, int]] = []
        self.adds_at: list[tuple[str, str]] = []          # (store, product)
        self.delivery_zip: str | None = "12345"

    @property
    def cart(self) -> dict[str, CartEntry]:
        """Every store's cart together (product names are unique in these tests)."""
        return {n: e for c in self.carts.values() for n, e in c.items()}

    async def list_stores(self):
        return [("meijer", "Meijer"), ("quicklly-grocery", "Quicklly Indian Grocery"), ("hmart", "HMart"),
                ("aldi", "ALDI"), ("cvs", "CVS")]

    async def search(self, store, query, item, limit=8):
        return self.catalog.get((store, query), [])

    async def add(self, store, query, product_name, packs):
        self.adds.append((product_name, packs))
        self.adds_at.append((store, product_name))
        c = self.prices[product_name]
        cart = self.carts.setdefault(store, {})
        e = cart.get(product_name)
        qty = max(packs, e.quantity if e else 0)
        cart[product_name] = CartEntry(product_name, c.size, round(c.price * qty, 2), qty, "ct")
        return qty

    async def cart_lines(self, store):
        return list(self.carts.get(store, {}).values())

    async def clear_cart(self, store):
        self.carts.get(store, {}).clear()
