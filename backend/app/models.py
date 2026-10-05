from typing import Any, Literal

from pydantic import BaseModel, Field

EffortLevel = Literal["low", "low_moderate", "moderate", "high"]
CookingSkill = Literal["basic", "intermediate", "advanced"]
Slot = Literal["breakfast", "mid_morning", "lunch", "evening", "dinner"]
SNACK_SLOTS: frozenset[str] = frozenset({"mid_morning", "evening"})

RunStatus = Literal["running", "awaiting_hitl", "cart_ready", "needs_attention", "failed"]
HitlKind = Literal[
    "plan_approval",
    "login_email",
    "login_password",
    "email_code",
    "captcha",
    "substitution_approval",
    "cart_clear_approval",
]


class MacroTargets(BaseModel):
    """Daily targets. Carbs and fat are computed for display but never gated."""

    calories: float = Field(gt=0)
    protein_g: float = Field(ge=0)


class MealSpec(BaseModel):
    user_id: str = "user_default"
    days: int = Field(default=7, ge=1, le=7)
    macros: MacroTargets
    cuisines: list[str] = []
    budget_weekly_usd: float | None = None
    zip_code: str = Field(pattern=r"^\d{5}$", description="US ZIP; checked against Instacart's delivery address")
    include_snacks: bool = True
    allergies: list[str] = []
    dietary_restrictions: list[str] = []
    dislikes: list[str] = []
    protein_source: str | None = None
    max_prep_minutes: int = Field(default=30, gt=0)
    effort_level: EffortLevel = "low_moderate"
    cooking_skill: CookingSkill = "basic"
    equipment: list[str] = ["stove", "microwave", "oven", "rice_cooker"]
    pantry: list[str] = []


class Ingredient(BaseModel):
    name: str
    grams: float = Field(gt=0)  # raw / dry weight, always
    preparation: str = ""


class Meal(BaseModel):
    slot: Slot
    title: str
    ingredients: list[Ingredient]
    steps: list[str] = []
    prep_minutes: int = Field(ge=0)
    equipment_used: list[str] = []
    notes: str = ""


class DayPlan(BaseModel):
    day: str
    meals: list[Meal]


class MealPlan(BaseModel):
    # Spec-dependent rules (snack slots, prep time, equipment, exclusions) are
    # gated in nutrition/validator.py so failures become repair hints.
    days: list[DayPlan]


class GroceryItem(BaseModel):
    name: str
    total_grams: float
    purchase_qty: str
    substitutes: list[str] = []
    est_price_usd: float | None = None


class GroceryList(BaseModel):
    items: list[GroceryItem]
    est_total_usd: float | None = None


class CartLine(BaseModel):
    product_name: str
    pack_size: str
    pack_grams: float
    packs: int
    unit_price_usd: float
    line_total_usd: float
    grocery_item: str
    substituted_for: str | None = None
    store: str = ""            # which store's cart this line is in


class CoverageRow(BaseModel):
    item: str
    grams_needed: float
    grams_in_cart: float
    covered: bool
    store: str = ""            # where it was bought; empty when not found


class CartReport(BaseModel):
    store: str                 # display name: "A" or "A + B"
    stores: list[str] = []     # every store with a cart, best first
    subtotals: dict[str, float] = {}
    lines: list[CartLine]
    coverage: list[CoverageRow]
    subtotal_usd: float
    currency: str = "USD"
    over_budget_by_usd: float | None = None
    notes: list[str] = []


class ValidationResult(BaseModel):
    passed: bool
    # per day: calories, protein_g (gated) and carbs_g, fat_g (informational)
    per_day: dict[str, dict[str, float]]
    # day -> one entry per meal, in plan order: {slot, calories, protein_g, carbs_g, fat_g,
    # ingredients: [{name, grams, calories, protein_g, carbs_g, fat_g}]}. For display and checking.
    per_meal: dict[str, list[dict[str, Any]]] = {}
    deltas: list[str] = []


class NLRunRequest(BaseModel):
    """Free-text alternative to posting a MealSpec; parsed by intake/intake.py."""

    text: str = Field(min_length=1)
