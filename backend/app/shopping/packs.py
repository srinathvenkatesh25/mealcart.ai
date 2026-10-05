"""Store pack sizes ↔ grams. The LLM never does this arithmetic.

parse_size() reads Instacart-style size text ("1.5 lb", "32 fl oz", "12 ct",
"each", "2 x 8 oz") into grams for one pack; packs_needed() rounds up; and
target_qty() renders a week's grams as something a shopper would buy.
"""

import math
import re

from app.nutrition.maps import canonical_key, contains_term

G_PER = {"lb": 453.592, "oz": 28.3495, "kg": 1000.0, "g": 1.0, "mg": 0.001}
ML_PER = {"fl_oz": 29.5735, "gal": 3785.41, "qt": 946.353, "pt": 473.176, "l": 1000.0, "ml": 1.0}

# grams per millilitre for foods sold by volume (default: water-like)
DENSITY = {"oil": 0.92, "ghee": 0.91, "milk": 1.03, "yogurt": 1.04, "curd": 1.04}

# typical grams for one unit of things sold by count
EACH_GRAMS = {
    "egg": 50, "onion": 150, "tomato": 120, "potato": 210, "sweet_potato": 200, "banana": 120,
    "apple": 180, "lemon": 60, "lime": 45, "bell_pepper": 160, "cucumber": 300, "garlic": 50,
    "avocado": 200, "cauliflower": 600, "cabbage": 900, "cilantro": 60, "spinach": 280,
    "orange": 140, "mango": 330, "eggplant": 450, "zucchini": 200,
}

SPICE_TERMS = ("turmeric", "cumin", "coriander_powder", "coriander_seed", "garam_masala", "chili_powder",
               "mustard_seed", "salt", "black_pepper", "cardamom", "cinnamon", "clove", "bay_leaf",
               "asafoetida", "hing", "curry_powder", "fenugreek", "ajwain", "paprika", "oregano",
               "chaat_masala", "kasuri_methi", "curry_leaf", "spice")
SPICE_MAX_WEEKLY_G = 60

_NUM = r"(\d+(?:\.\d+)?|\d+/\d+)"


def _num(text: str) -> float:
    if "/" in text:
        a, b = text.split("/")
        return float(a) / float(b)
    return float(text)


def each_grams(item: str) -> float | None:
    key = canonical_key(item)
    if key in EACH_GRAMS:
        return EACH_GRAMS[key]
    return next((g for name, g in EACH_GRAMS.items() if contains_term(key, name)), None)


def _density(item: str) -> float:
    return next((d for term, d in DENSITY.items() if contains_term(item, term)), 1.0)


def parse_size(text: str, item: str) -> float | None:
    """Grams in one pack, or None if the text can't be read."""
    t = text.lower().replace("fl. oz", "fl oz").replace("fluid ounce", "fl oz")
    t = re.sub(r"\bfl\s*oz\b", "fl_oz", t)
    t = re.sub(r"\b(gallons?)\b", "gal", t).replace("half gal", "0.5 gal")
    t = re.sub(r"\b(pounds?|lbs?)\b", "lb", t)
    t = re.sub(r"\b(ounces?)\b", "oz", t)
    t = re.sub(r"\b(count|ct|pack|pk|pcs?|pieces?)\b", "ct", t)
    t = re.sub(r"\b(litres?|liters?)\b", "l", t)

    multiplier = 1.0
    if m := re.search(rf"{_NUM}\s*(?:x|×)\s*", t):  # "2 x 8 oz"
        multiplier = _num(m.group(1))
        t = t[m.end():]

    if m := re.search(rf"{_NUM}\s*(lb|oz|kg|g|mg)\b", t):
        return multiplier * _num(m.group(1)) * G_PER[m.group(2)]
    if m := re.search(rf"{_NUM}\s*(fl_oz|gal|qt|pt|l|ml)\b", t):
        return multiplier * _num(m.group(1)) * ML_PER[m.group(2)] * _density(item)
    if "per lb" in t or t.strip() in {"lb", "/lb"}:
        return G_PER["lb"]
    unit = each_grams(item)
    if m := re.search(rf"{_NUM}\s*ct\b", t):
        return multiplier * _num(m.group(1)) * unit if unit else None
    if "dozen" in t:
        return 12 * multiplier * unit if unit else None
    if "each" in t or "bunch" in t:
        return multiplier * unit if unit else None
    return None


def packs_needed(needed_g: float, pack_g: float) -> int:
    return max(1, math.ceil(needed_g / pack_g - 1e-9))


def is_spice(item: str) -> bool:
    return any(contains_term(item, s) for s in SPICE_TERMS)


def target_qty(item: str, grams: float) -> str:
    """Human-readable amount for the approval screen; the shopper sizes real packs."""
    if is_spice(item) and grams <= SPICE_MAX_WEEKLY_G:
        return "1 small pack"
    unit = each_grams(item)
    if unit:
        count = math.ceil(grams / unit - 1e-9)
        if canonical_key(item) == "egg":
            return f"{count} eggs"
        return f"{count} × {item.replace('_', ' ')} (≈ {grams / G_PER['lb']:.1f} lb)"
    if any(contains_term(item, t) for t in ("milk", "oil")):  # sold by volume
        return f"≈ {math.ceil(grams / _density(item) / ML_PER['fl_oz'])} fl oz"
    if grams < G_PER["lb"]:
        return f"≈ {math.ceil(grams / G_PER['oz'])} oz"
    return f"≈ {math.ceil(grams / G_PER['lb'] * 4) / 4:g} lb"
