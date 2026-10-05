"""Realistic minimum cooking times, so the "max minutes per meal" limit means something.

The LLM labels "One-pot chicken pulao" as 5 minutes; raw chicken and rice alone need
about 15. A meal can't be faster than its slowest ingredient, so each meal's time is
raised to that floor (never lowered). If the floor then exceeds your limit, the
validator flags the meal and the planner swaps it.

These are deliberately conservative heuristics for a handful of slow foods, not a
cooking model. Anything not listed is left as the LLM wrote it.
"""

from dataclasses import dataclass

from app.models import Meal, MealPlan, MealSpec
from app.nutrition.maps import contains_term, normalize


@dataclass(frozen=True)
class Rule:
    terms: tuple[str, ...]
    minutes: int
    pressure_cooker_minutes: int | None = None  # if you own one
    exclude: tuple[str, ...] = ()               # words that mean it's not the raw food
    require: tuple[str, ...] = ()               # at least one of these must also appear


RULES: tuple[Rule, ...] = (
    Rule(("rice",), 15, exclude=("flour", "noodle", "vinegar", "paper", "cake", "cracker", "milk", "bran")),
    Rule(("brown_rice",), 30, pressure_cooker_minutes=20),
    Rule(("dal", "lentil", "split_pea", "moong", "masoor", "toor", "urad"), 20, pressure_cooker_minutes=12,
         exclude=("flour", "sprout", "canned")),
    Rule(("chickpea", "chana", "rajma", "kidney_bean", "black_bean", "pinto_bean", "navy_bean", "lima_bean",
          "black_eyed_pea"), 50, pressure_cooker_minutes=25,
         exclude=("canned", "sprout", "flour", "green"), require=("dry", "dried")),
    Rule(("chicken",), 15, exclude=("ground", "mince", "keema", "cooked", "rotisserie", "canned", "stock",
                                   "broth", "bouillon", "deli", "sausage")),
    Rule(("ground_chicken", "chicken_mince", "chicken_keema"), 10),
    Rule(("beef", "lamb", "mutton", "pork", "goat", "turkey"), 20,
         exclude=("ground", "cooked", "bacon", "sausage", "jerky", "broth", "stock")),
    Rule(("potato",), 15, exclude=("chip", "flour", "starch", "flake", "powder", "mashed")),
)


def _matches(ingredient: str, rule: Rule) -> bool:
    if any(contains_term(ingredient, x) for x in rule.exclude):
        return False
    if rule.require and not any(contains_term(ingredient, r) for r in rule.require):
        return False
    return any(contains_term(ingredient, t) for t in rule.terms)


def minimum_minutes(meal: Meal, equipment: list[str]) -> tuple[int, str | None]:
    """The slowest ingredient's floor in minutes, and which ingredient sets it."""
    has_pressure_cooker = "pressure_cooker" in {normalize(e) for e in equipment}
    floor, because = 0, None
    for ing in meal.ingredients:
        for rule in RULES:
            if _matches(ing.name, rule):
                minutes = (rule.pressure_cooker_minutes
                           if has_pressure_cooker and rule.pressure_cooker_minutes else rule.minutes)
                if minutes > floor:
                    floor, because = minutes, ing.name
    return floor, because


def apply_minimums(plan: MealPlan, spec: MealSpec) -> tuple[MealPlan, int]:
    """Raise each meal's prep_minutes to its floor; returns the plan and how many meals changed."""
    changed = 0
    out = plan.model_copy(deep=True)
    for day in out.days:
        for meal in day.meals:
            floor, because = minimum_minutes(meal, spec.equipment)
            if floor > meal.prep_minutes:
                note = f"Time raised to {floor} min: {because.replace('_', ' ')} needs at least that long."
                meal.notes = f"{meal.notes} {note}".strip()
                meal.prep_minutes = floor
                changed += 1
    return out, changed
