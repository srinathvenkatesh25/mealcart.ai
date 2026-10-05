// Mirrors backend/app/models.py and the events in backend/app/hitl/events.py.

export type EffortLevel = "low" | "low_moderate" | "moderate" | "high";
export type CookingSkill = "basic" | "intermediate" | "advanced";
export type Slot = "breakfast" | "mid_morning" | "lunch" | "evening" | "dinner";
export type HitlKind =
  | "plan_approval"
  | "login_email"
  | "login_password"
  | "email_code"
  | "captcha"
  | "substitution_approval"
  | "cart_clear_approval";
export type RunStatus = "running" | "awaiting_hitl" | "cart_ready" | "needs_attention" | "failed";

export interface MealSpec {
  days: number;
  macros: { calories: number; protein_g: number };
  cuisines: string[];
  budget_weekly_usd: number | null;
  zip_code: string;
  include_snacks: boolean;
  allergies: string[];
  dietary_restrictions: string[];
  dislikes: string[];
  protein_source: string | null;
  max_prep_minutes: number;
  effort_level: EffortLevel;
  cooking_skill: CookingSkill;
  equipment: string[];
  pantry: string[];
}

export interface Ingredient {
  name: string;
  grams: number;
  preparation: string;
}

export interface Meal {
  slot: Slot;
  title: string;
  ingredients: Ingredient[];
  steps: string[];
  prep_minutes: number;
  equipment_used: string[];
  notes: string;
}

export interface DayPlan {
  day: string;
  meals: Meal[];
}

export interface MealPlan {
  days: DayPlan[];
}

export interface DayTotals {
  calories: number;
  protein_g: number;
  carbs_g: number;
  fat_g: number;
  calories_delta: number;
  protein_g_delta: number;
}

export interface IngredientNutrition {
  name: string;
  grams: number;
  calories: number;
  protein_g: number;
  carbs_g: number;
  fat_g: number;
}

export interface MealNutrition {
  slot: Slot;
  calories: number;
  protein_g: number;
  carbs_g: number;
  fat_g: number;
  ingredients: IngredientNutrition[];
}

export interface Nutrition {
  per_day: Record<string, DayTotals>;
  per_meal: Record<string, MealNutrition[]>;
}

export interface GroceryItem {
  name: string;
  total_grams: number;
  purchase_qty: string;
  substitutes: string[];
  est_price_usd: number | null;
}

export interface GroceryList {
  items: GroceryItem[];
  est_total_usd: number | null;
}

export interface CartLine {
  product_name: string;
  pack_size: string;
  pack_grams: number;
  packs: number;
  unit_price_usd: number;
  line_total_usd: number;
  grocery_item: string;
  substituted_for: string | null;
  store?: string;
}

export interface CoverageRow {
  item: string;
  grams_needed: number;
  grams_in_cart: number;
  covered: boolean;
  store?: string;
}

export interface CartReport {
  store: string;
  stores?: string[];
  subtotals?: Record<string, number>;
  lines: CartLine[];
  coverage: CoverageRow[];
  subtotal_usd: number;
  currency: string;
  over_budget_by_usd: number | null;
  notes: string[];
}

export interface Question {
  event_id: string;
  kind: HitlKind;
  prompt: string;
  screenshot_b64?: string;
}

export interface SwapRequest {
  day: string;
  slot: Slot;
  reason: string;
  avoid: string[];
}

export type PlanApprovalValue = "approved" | "rejected" | { edit: GroceryList } | { swap: SwapRequest };

export type ServerEvent = { seq: number; run_id: string } & (
  | { type: "run.progress"; node: string; message: string }
  | {
      type: "plan.ready"; meal_plan: MealPlan; per_day: Record<string, DayTotals>;
      per_meal: Record<string, MealNutrition[]>; grocery_list: GroceryList;
    }
  | ({ type: "hitl.required" } & Question)
  | { type: "hitl.resolved"; event_id: string; kind: HitlKind }
  | { type: "hitl.rejected"; kind: HitlKind; reason: string }
  | { type: "cart.ready"; report: CartReport }
  | { type: "run.needs_attention"; report: CartReport }
  | { type: "run.failed"; error: string; detail: string[] }
);

export interface LlmUsage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  models: { model: string; calls: number; tokens: number }[];
}

export interface RunRow {
  id: string;
  status: RunStatus;
  meal_spec_json: MealSpec;
  meal_plan_json: MealPlan | null;
  grocery_list_json: GroceryList | null;
  cart_report_json: CartReport | null;
  nutrition_json: Nutrition | null;
  error: string | null;
  open_hitl_event: { id: string; kind: HitlKind; payload: { prompt: string } } | null;
  last_seq: number;
  llm_usage: LlmUsage;
}
