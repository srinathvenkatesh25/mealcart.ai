"""USDA FoodData Central lookup with a SQLite cache.

All gram weights in the app are raw/dry, so searches add "raw" and prefer
entries described as raw over cooked/fried/breaded ones.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from app import db
from app.config import get_settings
from app.nutrition.maps import ALIAS, MANUAL_MACROS, normalize, singular

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
DATA_TYPES = ["Foundation", "SR Legacy"]

# FDC nutrient ids. Foundation foods often report energy only as Atwater (2047/2048).
ENERGY_IDS = (1008, 2047, 2048)
PROTEIN_ID, FAT_ID, CARBS_ID = 1003, 1004, 1005

AVOID_WORDS = frozenset({"cooked", "fried", "breaded", "roasted", "boiled", "baked", "braised",
                         "grilled", "stewed", "microwaved", "canned", "drained"})


class UnknownIngredientError(Exception):
    def __init__(self, name: str):
        super().__init__(f"no USDA match for '{name}'")
        self.name = name


class FoodMacros(BaseModel):
    """Per 100 g."""

    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fdc_id: int
    description: str


MacroLookup = Callable[[str], Awaitable[FoodMacros]]


def _nutrient(food: dict, nutrient_id: int) -> float | None:
    for n in food.get("foodNutrients", []):
        if n.get("nutrientId") == nutrient_id and n.get("value") is not None:
            if nutrient_id in ENERGY_IDS and str(n.get("unitName", "")).upper() != "KCAL":
                continue
            return float(n["value"])
    return None


def _to_macros(food: dict) -> FoodMacros | None:
    kcal = next((v for i in ENERGY_IDS if (v := _nutrient(food, i)) is not None), None)
    protein = _nutrient(food, PROTEIN_ID)
    if kcal is None or protein is None:
        return None
    return FoodMacros(
        calories=kcal,
        protein_g=protein,
        carbs_g=_nutrient(food, CARBS_ID) or 0.0,
        fat_g=_nutrient(food, FAT_ID) or 0.0,
        fdc_id=int(food["fdcId"]),
        description=food.get("description", ""),
    )


# Descriptive words that say nothing about which food it is; "ground" alone
# must not let "Bison, ground, raw" win a search for ground turmeric.
GENERIC_WORDS = frozenset({
    "raw", "ground", "whole", "fresh", "dry", "dried", "frozen", "plain", "boneless", "skinless",
    "nonfat", "lowfat", "regular", "meat", "only", "and", "or", "with", "without", "the",
})


def food_words(text: str) -> set[str]:
    return {singular(w) for w in re.findall(r"[a-z]+", text.lower())}


def _raw_score(words: set[str]) -> int:
    return (2 if "raw" in words else 0) - (3 if words & AVOID_WORDS else 0)


def pick_best(foods: list[dict], about: str) -> FoodMacros | None:
    """Best usable result for the food named by `about`.

    A result must share at least one non-generic word with `about`. Rank by
    shared words, then whether USDA's head name (text before the first comma)
    is exactly our food ("Apples" beats "Rose-apples", "Sweet potato" beats
    "Sweet potato leaves"), then raw-ness, then USDA's own relevance order.
    """
    key_words = food_words(about) - GENERIC_WORDS
    ranked = []
    for i, f in enumerate(foods):
        m = _to_macros(f)
        if m is None:
            continue
        words = food_words(m.description)
        overlap = len(key_words & words)
        if key_words and overlap == 0:
            continue
        head = food_words(m.description.split(",")[0]) - GENERIC_WORDS
        head_match = bool(head) and head <= key_words
        ranked.append(((-overlap, -head_match, -_raw_score(words), i), m))
    return min(ranked, key=lambda t: t[0])[1] if ranked else None


class USDAClient:
    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.AsyncClient | None = None,
        retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.api_key = api_key if api_key is not None else get_settings().usda_api_key
        self.http = http or httpx.AsyncClient(timeout=15)
        self.retries = retries
        self.sleep = sleep

    async def _search(self, query: str) -> list[dict]:
        body = {"query": query, "dataType": DATA_TYPES, "pageSize": 10}
        for attempt in range(self.retries):
            try:
                resp = await self.http.post(SEARCH_URL, params={"api_key": self.api_key}, json=body)
            except httpx.TransportError:
                if attempt == self.retries - 1:
                    raise
            else:
                if resp.status_code != 429 and resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.json().get("foods", [])
                if attempt == self.retries - 1:
                    resp.raise_for_status()
            await self.sleep(2**attempt)
        return []

    async def get_macros_per_100g(self, canonical_name: str) -> FoodMacros:
        key = normalize(canonical_name)
        if key in MANUAL_MACROS:
            return FoodMacros.model_validate(MANUAL_MACROS[key])
        cached = await db.get_usda_cache(key)
        if cached is not None:
            return FoodMacros.model_validate(cached)

        if key in ALIAS:
            queries = [ALIAS[key]]
        else:
            plain = key.replace("_", " ")
            queries = [f"{plain} raw", plain]

        for query in queries:
            # Aliases name the USDA food (toor_dal -> "pigeon peas"), so match on the query.
            best = pick_best(await self._search(query), about=query)
            if best is not None:
                await db.put_usda_cache(key, best.model_dump())
                return best
        raise UnknownIngredientError(canonical_name)
