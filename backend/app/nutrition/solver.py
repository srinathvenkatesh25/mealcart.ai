"""Deterministic portion sizing: the LLM picks foods, this picks grams.

For each day, scale every ingredient by a factor within bounds so the day's
calories and protein land on target, changing the LLM's portions as little as
possible (bounded least squares with a pull toward scale 1).
"""

import math

import numpy as np
from scipy.optimize import lsq_linear

from app.models import DayPlan, MealPlan, MealSpec
from app.nutrition.maps import contains_term
from app.nutrition.usda import FoodMacros, MacroLookup, UnknownIngredientError
from app.nutrition.validator import CALORIE_TOLERANCE, PROTEIN_TOLERANCE_G

SCALE_MIN, SCALE_MAX = 0.5, 2.0
FIXED_MAX_G = 10           # spices, chilies, garlic: keep as written
FAT_TERMS = ("oil", "ghee", "butter")
FAT_MAX_PER_MEAL_G = 30
ROUND_TO_G = 5
# Hitting targets dominates; among portions that hit them, prefer the ones
# closest to what the LLM chose.
TARGET_WEIGHT = 20.0
STAY_CLOSE_WEIGHT = 0.05


def _is_fat(name: str) -> bool:
    return any(contains_term(name, t) for t in FAT_TERMS)


def _round_grams(grams: float) -> float:
    return max(ROUND_TO_G, ROUND_TO_G * round(grams / ROUND_TO_G))


def day_totals(day: DayPlan, macros: dict[str, FoodMacros]) -> tuple[float, float]:
    kcal = sum(i.grams / 100 * macros[i.name].calories for m in day.meals for i in m.ingredients)
    protein = sum(i.grams / 100 * macros[i.name].protein_g for m in day.meals for i in m.ingredients)
    return kcal, protein


def within_targets(kcal: float, protein: float, spec: MealSpec) -> bool:
    t = spec.macros
    return (abs(kcal - t.calories) / t.calories <= CALORIE_TOLERANCE
            and abs(protein - t.protein_g) <= PROTEIN_TOLERANCE_G)


async def solve_day(day: DayPlan, spec: MealSpec, lookup: MacroLookup) -> tuple[DayPlan, bool]:
    """Return the re-portioned day and whether it now hits both targets.

    Days with an unknown ingredient are returned unchanged; the validator
    turns that into a repair hint.
    """
    macros: dict[str, FoodMacros] = {}
    try:
        for meal in day.meals:
            for ing in meal.ingredients:
                if ing.name not in macros:
                    macros[ing.name] = await lookup(ing.name)
    except UnknownIngredientError:
        return day, False

    t_kcal, t_prot = spec.macros.calories, max(spec.macros.protein_g, 1.0)
    variables, rows_kcal, rows_prot, lo, hi = [], [], [], [], []
    fixed_kcal = fixed_prot = 0.0

    for mi, meal in enumerate(day.meals):
        fat_grams = sum(i.grams for i in meal.ingredients if _is_fat(i.name))
        for ii, ing in enumerate(meal.ingredients):
            m = macros[ing.name]
            kcal, prot = ing.grams / 100 * m.calories, ing.grams / 100 * m.protein_g
            if ing.grams <= FIXED_MAX_G:
                fixed_kcal += kcal
                fixed_prot += prot
                continue
            upper = SCALE_MAX
            if _is_fat(ing.name) and fat_grams > 0:
                upper = max(SCALE_MIN, min(SCALE_MAX, FAT_MAX_PER_MEAL_G / fat_grams))
            variables.append((mi, ii))
            rows_kcal.append(kcal / t_kcal)
            rows_prot.append(prot / t_prot)
            lo.append(SCALE_MIN)
            hi.append(upper)

    if not variables:
        kcal, prot = day_totals(day, macros)
        return day, within_targets(kcal, prot, spec)

    n = len(variables)
    reg = math.sqrt(STAY_CLOSE_WEIGHT)
    targets = np.array([1 - fixed_kcal / t_kcal, 1 - fixed_prot / t_prot])
    A = np.vstack([TARGET_WEIGHT * np.array([rows_kcal, rows_prot]), reg * np.eye(n)])
    b = np.concatenate([TARGET_WEIGHT * targets, reg * np.ones(n)])
    result = lsq_linear(A, b, bounds=(np.array(lo), np.array(hi)))

    new_day = day.model_copy(deep=True)
    for (mi, ii), scale in zip(variables, result.x):
        ing = new_day.meals[mi].ingredients[ii]
        ing.grams = _round_grams(ing.grams * float(scale))

    kcal, prot = day_totals(new_day, macros)
    return new_day, within_targets(kcal, prot, spec)


async def solve_plan(plan: MealPlan, spec: MealSpec, lookup: MacroLookup,
                     only: list[str] | None = None) -> tuple[MealPlan, list[str]]:
    """Re-portion every day (or only the named days); returns the plan and the days that miss targets."""
    days, missed = [], []
    for day in plan.days:
        if only is not None and day.day not in only:
            days.append(day)
            continue
        solved, hit = await solve_day(day, spec, lookup)
        days.append(solved)
        if not hit:
            missed.append(day.day)
    return MealPlan(days=days), missed
