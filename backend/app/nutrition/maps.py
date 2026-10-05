"""Hand-checked lookup tables and the one matching rule everything uses.

Matching is token-based on canonical snake_case names, so "egg" matches
"egg_whites" but not "eggplant", and "mushrooms" matches "button_mushroom".
"""

import re

# canonical name -> USDA search query. Only staples whose plain search tends to
# return the wrong food (cooked, branded, a different cut) need an entry.
ALIAS: dict[str, str] = {
    "chicken_breast": "chicken broilers or fryers breast meat only raw",
    "chicken_breast_boneless_skinless": "chicken broilers or fryers breast meat only raw",
    "chicken_thigh": "chicken broilers or fryers thigh meat only raw",
    "chicken_thigh_boneless_skinless": "chicken broilers or fryers thigh meat only raw",
    "ground_chicken": "chicken ground raw",
    "egg": "egg whole raw fresh",
    "eggs": "egg whole raw fresh",
    "egg_white": "egg white raw fresh",
    "basmati_rice": "rice white long-grain regular raw enriched",
    "white_rice": "rice white long-grain regular raw enriched",
    "brown_rice": "rice brown long-grain raw",
    "rolled_oats": "oats regular and quick not fortified dry",
    "oats": "oats regular and quick not fortified dry",
    "atta_whole_wheat": "flour whole wheat",
    "whole_wheat_flour": "flour whole wheat",
    "toor_dal": "pigeon peas red gram mature seeds raw",
    "masoor_dal": "lentils pink or red raw",
    "moong_dal": "mung beans mature seeds raw",
    "chickpeas_dry": "chickpeas garbanzo beans mature seeds raw",
    "greek_yogurt_nonfat": "yogurt greek plain nonfat",
    "plain_yogurt": "yogurt plain whole milk",
    "milk_2_percent": "milk reduced fat fluid 2% milkfat",
    "ghee": "butter oil anhydrous",
    "vegetable_oil": "oil vegetable canola",
    "canola_oil": "oil canola",
    "olive_oil": "oil olive salad or cooking",
    "onion": "onions raw",
    "tomato": "tomatoes red ripe raw year round average",
    "potato": "potatoes flesh and skin raw",
    "spinach": "spinach raw",
    "banana": "bananas raw",
    "peanut_butter": "peanut butter smooth style without salt",
    "whole_wheat_bread": "bread whole-wheat commercially prepared",
    # Spice blends: USDA has no garam masala outside inconsistent brand labels;
    # curry powder is the closest generic blend, and grams used are tiny.
    "garam_masala": "spices curry powder",
    "curry_powder": "spices curry powder",
    "coriander_powder": "spices coriander seed",
    "cumin_powder": "spices cumin seed",
    "red_chili_powder": "spices chili powder",
    "chili_powder": "spices chili powder",
    "curd": "yogurt plain whole milk",
    "dahi": "yogurt plain whole milk",
    "lemon": "lemons raw without peel",
}

# Foods missing from USDA's Foundation/SR Legacy data. Values are per 100 g,
# copied from a USDA Branded Foods label (fdc_id cited), hand-checked against
# a second brand where possible.
MANUAL_MACROS: dict[str, dict] = {
    # FDC 2429587 (Karoun) and 2597884 (Atalanta) both list 321 kcal, 25 g protein.
    "paneer": {"calories": 321, "protein_g": 25.0, "carbs_g": 3.57, "fat_g": 25.0,
               "fdc_id": 2429587, "description": "Paneer (USDA Branded label, Karoun Dairies)"},
}

# allergen -> terms that indicate it, plus phrases that look like a hit but aren't.
ALLERGEN_MAP: dict[str, dict[str, list[str]]] = {
    "dairy": {
        "terms": ["milk", "ghee", "butter", "paneer", "curd", "yogurt", "yoghurt", "cheese",
                  "cream", "whey", "casein", "khoa", "buttermilk", "lassi"],
        "except": ["coconut_milk", "almond_milk", "oat_milk", "soy_milk", "peanut_butter",
                   "almond_butter", "cashew_butter", "cocoa_butter", "coconut_cream"],
    },
    "egg": {"terms": ["egg", "mayonnaise", "mayo"], "except": []},
    "peanuts": {"terms": ["peanut", "groundnut"], "except": []},
    "tree_nuts": {
        "terms": ["almond", "cashew", "walnut", "pistachio", "pecan", "hazelnut",
                  "macadamia", "brazil_nut", "pine_nut"],
        "except": [],
    },
    "gluten": {
        "terms": ["wheat", "atta", "maida", "semolina", "sooji", "rava", "barley", "rye",
                  "bread", "pasta", "noodle", "couscous", "seitan", "roti", "naan", "tortilla"],
        "except": ["buckwheat", "corn_tortilla", "rice_noodle", "gluten_free"],
    },
    "soy": {"terms": ["soy", "soya", "tofu", "tempeh", "edamame", "miso"], "except": []},
    "fish": {"terms": ["fish", "salmon", "tuna", "cod", "tilapia", "sardine", "anchovy", "mackerel"],
             "except": []},
    "shellfish": {"terms": ["shrimp", "prawn", "crab", "lobster", "scallop", "clam", "mussel", "oyster"],
                  "except": []},
    "sesame": {"terms": ["sesame", "tahini", "til"], "except": []},
}

_MEATS = ["chicken", "beef", "pork", "lamb", "mutton", "goat", "turkey", "duck", "veal",
          "bacon", "ham", "sausage", "steak", "keema", "gelatin"]
_SEAFOOD = ALLERGEN_MAP["fish"]["terms"] + ALLERGEN_MAP["shellfish"]["terms"]
_PORK = ["pork", "bacon", "ham", "lard", "prosciutto", "pancetta", "chorizo"]

RESTRICTION_MAP: dict[str, dict[str, list[str]]] = {
    "vegetarian": {"terms": _MEATS + _SEAFOOD, "except": []},
    "pescatarian": {"terms": _MEATS, "except": []},
    "vegan": {
        "terms": _MEATS + _SEAFOOD + ALLERGEN_MAP["dairy"]["terms"] + ["egg", "honey", "mayonnaise", "mayo"],
        "except": ALLERGEN_MAP["dairy"]["except"],
    },
    "halal": {"terms": _PORK + ["wine", "beer", "rum", "gelatin"], "except": []},
    "no_beef": {"terms": ["beef", "steak", "veal"], "except": []},
    "no_pork": {"terms": _PORK, "except": []},
}

# dislike -> extra names for the same food
DISLIKE_SYNONYMS: dict[str, list[str]] = {
    "mayonnaise": ["mayo"],
    "mushroom": ["shiitake", "portobello", "cremini", "button_mushroom"],
    "cilantro": ["coriander_leaves"],
    "eggplant": ["brinjal", "aubergine"],
}


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("oes"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(name: str) -> list[str]:
    return [singular(t) for t in normalize(name).split("_") if t]


def contains_term(ingredient: str, term: str) -> bool:
    """True if the term's tokens appear contiguously in the ingredient's tokens."""
    ing, t = _tokens(ingredient), _tokens(term)
    if not t:
        return False
    return any(ing[i : i + len(t)] == t for i in range(len(ing) - len(t) + 1))


def _match_entry(ingredient: str, entry: dict[str, list[str]]) -> str | None:
    if any(contains_term(ingredient, e) for e in entry["except"]):
        return None
    return next((t for t in entry["terms"] if contains_term(ingredient, t)), None)


def _fallback_terms(label: str) -> list[str]:
    """Unknown labels match themselves, minus a leading 'no_' (e.g. 'no_lamb' -> 'lamb')."""
    key = normalize(label)
    return [key[3:] if key.startswith("no_") else key]


def allergen_hit(ingredient: str, allergy: str) -> str | None:
    entry = ALLERGEN_MAP.get(normalize(allergy)) or {"terms": _fallback_terms(allergy), "except": []}
    return _match_entry(ingredient, entry)


def restriction_hit(ingredient: str, restriction: str) -> str | None:
    entry = RESTRICTION_MAP.get(normalize(restriction)) or {"terms": _fallback_terms(restriction), "except": []}
    return _match_entry(ingredient, entry)


def dislike_hit(ingredient: str, dislike: str) -> str | None:
    key = normalize(dislike)
    terms = [key] + DISLIKE_SYNONYMS.get(singular(key), []) + DISLIKE_SYNONYMS.get(key, [])
    return next((t for t in terms if contains_term(ingredient, t)), None)


def exclusion_hits(ingredient: str, allergies: list[str], restrictions: list[str],
                   dislikes: list[str]) -> list[tuple[str, str]]:
    """Every (kind, label) the ingredient violates, e.g. [("allergy", "dairy")]."""
    checks = ([("allergy", a, allergen_hit) for a in allergies]
              + [("restriction", r, restriction_hit) for r in restrictions]
              + [("dislike", d, dislike_hit) for d in dislikes])
    return [(kind, label) for kind, label, match in checks if match(ingredient, label)]


def canonical_key(name: str) -> str:
    """Merge spelling variants of one food: 'Eggs' and 'egg' -> 'egg'."""
    return "_".join(_tokens(name))


def forbidden_terms(label: str, kind: str) -> list[str]:
    """The ingredient words a restriction, allergy or dislike rules out, for the planner prompt.

    The LLM is told "vegetarian" as the list of foods it excludes ("chicken, beef, ..."),
    so it doesn't have to guess what the label means.
    """
    table = {"allergy": ALLERGEN_MAP, "restriction": RESTRICTION_MAP}.get(kind, {})
    entry = table.get(normalize(label))
    if entry:
        return list(dict.fromkeys(entry["terms"]))
    key = normalize(label)
    base = key[3:] if key.startswith("no_") else key
    return [base] + DISLIKE_SYNONYMS.get(singular(base), []) + DISLIKE_SYNONYMS.get(base, [])
