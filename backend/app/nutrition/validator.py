"""Deterministic plan checks. The LLM never estimates nutrition.

Gated: daily calories (±5%) and protein (±10 g), exclusions (allergies,
restrictions, dislikes), prep time, equipment, step count for basic cooks,
snack slots, day count and weekly variety (no meal more than twice). Carbs and fat are computed for display only.

Every failure becomes one mechanical repair hint naming the day and meal.
"""

from app.models import SNACK_SLOTS, DayPlan, MealPlan, MealSpec, ValidationResult
from app.nutrition.maps import exclusion_hits, normalize
from app.nutrition.usda import FoodMacros, MacroLookup, UnknownIngredientError

CALORIE_TOLERANCE = 0.05
PROTEIN_TOLERANCE_G = 10.0
BASIC_MAX_STEPS = 8
# Twice allows "cook once, eat the leftovers"; more than that is a monotonous week.
MAX_SAME_MEAL_PER_WEEK = 2

# Hand tools and cookware are not "equipment" in the spec's sense.
HAND_TOOLS = frozenset({
    "knife", "cutting_board", "bowl", "mixing_bowl", "spoon", "spatula", "whisk", "ladle",
    "pot", "pan", "skillet", "frying_pan", "saucepan", "kadai", "kadhai", "tawa", "wok",
    "baking_sheet", "sheet_pan", "baking_dish", "colander", "strainer", "grater", "peeler",
    "measuring_cup", "measuring_cups", "measuring_spoons", "tongs", "lid", "plate",
})


def _round10(grams: float) -> int:
    return max(10, int(round(grams / 10.0)) * 10)


def _exclusion_hints(day: DayPlan, spec: MealSpec) -> list[str]:
    return [
        f"{day.day} {meal.slot}: '{ing.name}' violates {kind} '{label}' — replace it"
        for meal in day.meals
        for ing in meal.ingredients
        for kind, label in exclusion_hits(ing.name, spec.allergies, spec.dietary_restrictions, spec.dislikes)
    ]


def _meal_rule_hints(day: DayPlan, spec: MealSpec) -> list[str]:
    hints = []
    allowed_equipment = {normalize(e) for e in spec.equipment}
    for meal in day.meals:
        where = f"{day.day} {meal.slot} ('{meal.title}')"
        if not spec.include_snacks and meal.slot in SNACK_SLOTS:
            hints.append(f"{where}: snacks are turned off — remove this meal")
        if meal.prep_minutes > spec.max_prep_minutes:
            hints.append(
                f"{where}: takes {meal.prep_minutes} min, limit is {spec.max_prep_minutes} — simplify or swap it"
            )
        missing = sorted(
            e for e in (normalize(x) for x in meal.equipment_used)
            if e not in allowed_equipment and e not in HAND_TOOLS
        )
        if missing:
            hints.append(f"{where}: needs {', '.join(missing)}, which is not available — use only {sorted(allowed_equipment)}")
        if spec.cooking_skill == "basic" and len(meal.steps) > BASIC_MAX_STEPS:
            hints.append(f"{where}: {len(meal.steps)} steps is too many for a basic cook — keep it to {BASIC_MAX_STEPS}")
    return hints


def _macro_hints(
    day: str, totals: dict[str, float], contrib: dict[str, dict[str, float]],
    per100: dict[str, FoodMacros], targets: dict[str, float],
) -> list[str]:
    hints = []
    for key, unit, ok in (
        ("calories", "kcal", abs(totals["calories"] - targets["calories"]) / targets["calories"] <= CALORIE_TOLERANCE),
        ("protein_g", "g", abs(totals["protein_g"] - targets["protein_g"]) <= PROTEIN_TOLERANCE_G),
    ):
        if ok:
            continue
        delta = totals[key] - targets[key]
        # Biggest contributor of this nutrient that actually has some of it.
        name = max(contrib, key=lambda n: contrib[n][key])
        density = getattr(per100[name], key) / 100.0
        label = "calories" if key == "calories" else "protein"
        direction, verb = ("over", "reduce") if delta > 0 else ("under", "increase")
        change = f" — {verb} it by ~{_round10(abs(delta) / density)}g" if density > 0 else ""
        hints.append(
            f"{day}: {label} {abs(delta):.0f}{unit} {direction} target "
            f"({name} is the largest contributor){change}"
        )
    return hints


def _variety_hints(plan: MealPlan) -> list[str]:
    seen: dict[str, list[str]] = {}
    hints = []
    for day in plan.days:
        for meal in day.meals:
            key = " ".join(meal.title.lower().split())
            earlier = seen.setdefault(key, [])
            if len(earlier) >= MAX_SAME_MEAL_PER_WEEK:
                hints.append(
                    f"{day.day} {meal.slot}: '{meal.title}' already appears on {', '.join(earlier)} — "
                    "choose a different meal"
                )
            earlier.append(day.day)
    return hints


async def check(plan: MealPlan, spec: MealSpec, lookup: MacroLookup) -> ValidationResult:
    targets = {"calories": spec.macros.calories, "protein_g": spec.macros.protein_g}
    deltas: list[str] = []
    per_day: dict[str, dict[str, float]] = {}
    per_meal: dict[str, list[dict]] = {}

    if len(plan.days) != spec.days:
        deltas.append(f"plan has {len(plan.days)} days, expected {spec.days}")

    cache: dict[str, FoodMacros | None] = {}

    for day in plan.days:
        totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        contrib: dict[str, dict[str, float]] = {}
        unknown = False
        day_meals: list[dict] = []

        for meal in day.meals:
            meal_totals = dict.fromkeys(totals, 0.0)
            rows: list[dict] = []
            for ing in meal.ingredients:
                if ing.name not in cache:
                    try:
                        cache[ing.name] = await lookup(ing.name)
                    except UnknownIngredientError:
                        cache[ing.name] = None
                macros = cache[ing.name]
                if macros is None:
                    unknown = True
                    deltas.append(
                        f"{day.day} {meal.slot}: '{ing.name}' has no USDA entry — "
                        "rename it to a common grocery name or replace it"
                    )
                    continue
                c = contrib.setdefault(ing.name, dict.fromkeys(totals, 0.0))
                row = {"name": ing.name, "grams": ing.grams}
                for k in totals:
                    amount = ing.grams / 100.0 * getattr(macros, k)
                    totals[k] += amount
                    meal_totals[k] += amount
                    c[k] += amount
                    row[k] = round(amount, 1)
                rows.append(row)
            day_meals.append({"slot": meal.slot, **{k: round(v, 1) for k, v in meal_totals.items()},
                              "ingredients": rows})
        per_meal[day.day] = day_meals

        per_day[day.day] = {
            **{k: round(v, 1) for k, v in totals.items()},
            "calories_delta": round(totals["calories"] - targets["calories"], 1),
            "protein_g_delta": round(totals["protein_g"] - targets["protein_g"], 1),
        }
        if not unknown and contrib:
            per100 = {n: cache[n] for n in contrib}
            deltas += _macro_hints(day.day, totals, contrib, per100, targets)
        elif not contrib:
            deltas.append(f"{day.day}: no ingredients")
        deltas += _exclusion_hints(day, spec)
        deltas += _meal_rule_hints(day, spec)

    deltas += _variety_hints(plan)
    return ValidationResult(passed=not deltas, per_day=per_day, per_meal=per_meal, deltas=deltas)
