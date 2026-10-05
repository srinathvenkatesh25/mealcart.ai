from typing import TypedDict

from app.models import GroceryList, MealPlan, MealSpec, ValidationResult


class RunState(TypedDict, total=False):
    run_id: str
    spec: MealSpec
    warnings: list[str]
    meal_plan: MealPlan | None
    validation: ValidationResult | None
    repair_attempts: int
    grocery_list: GroceryList | None
    error: str | None
