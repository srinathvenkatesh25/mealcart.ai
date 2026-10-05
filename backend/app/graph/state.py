from typing import TypedDict

from app.models import CartReport, GroceryList, MealPlan, MealSpec, ValidationResult


class RunState(TypedDict, total=False):
    run_id: str
    spec: MealSpec
    warnings: list[str]
    meal_plan: MealPlan | None
    validation: ValidationResult | None
    repair_attempts: int
    grocery_list: GroceryList | None
    cart_report: CartReport | None
    swap_request: dict | None  # {day, slot, reason?, avoid?} from the approval step
    swap_failed: bool
    solve_days: list[str] | None  # after a swap, re-size only that day
    error: str | None
