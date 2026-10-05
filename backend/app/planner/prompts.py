"""Prompt text for the planner. The LLM picks foods; software sets exact grams."""

import json

from app.models import DayPlan, MealSpec
from app.nutrition.maps import ALIAS, MANUAL_MACROS

SKILL_GUIDANCE = {
    "basic": "Basic cook: one-pot, sheet-pan, rice-cooker or microwave meals; no deep-frying, "
             "no dough shaping beyond simple rotis, at most 8 short steps.",
    "intermediate": "Intermediate cook: standard techniques are fine; at most 12 steps.",
    "advanced": "Advanced cook: any technique.",
}
EFFORT_GUIDANCE = {
    "low": "Low effort: minimal chopping, reuse the same base (e.g. a dal or curry) across meals.",
    "low_moderate": "Low-to-moderate effort: simple prep; batch-cook staples like rice and dal.",
    "moderate": "Moderate effort: normal home cooking.",
    "high": "High effort is fine.",
}

PLANNER_SYSTEM = """You plan home-cooked meals and reply only with JSON.

Hard rules (a program checks every one; violations are rejected):
- Never use these ingredients or anything containing them: {exclusions}.
- Every meal takes at most {max_prep} minutes (prep + cooking) — set prep_minutes honestly.
- equipment_used lists only appliances from: {equipment}. Pots, pans and knives need not be listed.
- {skill}
- Meals for these slots only, one each: {slots}.
- No meal (same title) more than twice in the week; leftovers the next day are fine.

Ingredient rules:
- Give every ingredient a canonical snake_case name and a RAW / DRY gram weight
  (uncooked rice, dry lentils, raw meat). Never cooked weights.
- Always list cooking oil or ghee as its own ingredient.
- Use common grocery ingredients with USDA nutrition entries. No brand names.
- Prefer these names when they fit: {vocabulary}.
- Put each preparation note (diced, minced) in `preparation`, not in the name.

Portions: aim near {calories:.0f} kcal and {protein:.0f} g protein for the whole day.
Software will fine-tune gram weights afterwards, so pick sensible foods rather than
doing arithmetic.

Style: {effort} Cuisines: {cuisines}. {protein_source}{budget}
Write short numbered-style steps (one action each)."""

PLANNER_USER = """Plan these days: {days}. Vary meals across days but reuse ingredients so the
grocery list stays short.
{reuse}
Return a MealPlan with exactly these days, in order: {days}."""

REPAIR_USER = """These days were rejected. Fix every problem listed for each day and return a
MealPlan with exactly these days, in order: {days}. Keep everything not mentioned the same.

{problems}"""


def _slots(spec: MealSpec) -> list[str]:
    return (["breakfast", "mid_morning", "lunch", "evening", "dinner"] if spec.include_snacks
            else ["breakfast", "lunch", "dinner"])


def planner_system(spec: MealSpec) -> str:
    exclusions = spec.allergies + spec.dietary_restrictions + spec.dislikes
    return PLANNER_SYSTEM.format(
        exclusions=", ".join(exclusions) if exclusions else "(none)",
        max_prep=spec.max_prep_minutes,
        equipment=", ".join(spec.equipment),
        skill=SKILL_GUIDANCE[spec.cooking_skill],
        slots=", ".join(_slots(spec)),
        vocabulary=", ".join(sorted({*ALIAS, *MANUAL_MACROS})),
        calories=spec.macros.calories,
        protein=spec.macros.protein_g,
        effort=EFFORT_GUIDANCE[spec.effort_level],
        cuisines=", ".join(spec.cuisines) if spec.cuisines else "any",
        protein_source=f"Main protein source: {spec.protein_source}. " if spec.protein_source else "",
        budget=(f"Weekly grocery budget about ${spec.budget_weekly_usd:.0f}: favour inexpensive staples."
                if spec.budget_weekly_usd else ""),
    )


def planner_user(days: list[str], earlier: list[DayPlan]) -> str:
    if earlier:
        titles = [m.title for d in earlier for m in d.meals]
        ingredients = sorted({i.name for d in earlier for m in d.meals for i in m.ingredients})
        reuse = (f"Earlier days already use: {', '.join(ingredients)}. Reuse these where sensible so the "
                 f"grocery list stays short, but do not repeat these exact meals: {'; '.join(titles)}.")
    else:
        reuse = ""
    return PLANNER_USER.format(days=", ".join(days), reuse=reuse)


def repair_user(failing: list[DayPlan], hints: dict[str, list[str]]) -> str:
    problems = "\n\n".join(
        f"{d.day} problems:\n" + "\n".join(f"- {h}" for h in hints[d.day])
        + f"\n{d.day} current plan:\n{json.dumps(d.model_dump(), indent=1)}"
        for d in failing
    )
    return REPAIR_USER.format(days=", ".join(d.day for d in failing), problems=problems)
