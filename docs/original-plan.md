# MealCart Agent — End-to-End Build Plan

> **Goal:** A single-user web app that takes natural-language nutrition requirements,
> generates a macro-accurate 7-day meal plan, and uses a browser-automation agent to
> log into Instacart and fill the grocery cart. The agent **stops at a filled cart** —
> it never checks out or pays. No MCP involved; the Instacart integration is
> Playwright + Browser-Use driving a real Chromium session.
>
> **How to use this document:** It is written as a build specification for an AI
> coding assistant (in VS Code). Implement top-to-bottom in the phase
> order given in §16. Every component has exact file paths, schemas, and acceptance
> criteria.

---

## 1. Pipeline overview

```
User requirements (chat)
  → MealSpec (structured JSON)
  → Meal planner (LLM, structured output)
  → Macro validator (deterministic, USDA data) ──repair loop, max 3──┐
  → Grocery list consolidator (deterministic)                        │
  → Shopping agent (Browser-Use + Playwright, persistent profile)    │
  → Cart verification (deterministic read-back)                      │
  → User reviews filled cart in chat → user checks out manually ◄────┘
```

Human-in-the-loop (HITL) pauses can occur at: plan approval (optional gate),
CAPTCHA/code challenges during login, and substitution approvals when a swap
breaks macro tolerance.

## 2. Tech stack (final, do not substitute without reason)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| API | FastAPI + uvicorn[standard] | REST + websocket on one server |
| Orchestration | LangGraph | `StateGraph`, `interrupt()` for HITL |
| Browser driver | Playwright (Python) | Persistent contexts = saved logins |
| Shopping agent | `browser-use` (pip, MIT license, free) | LLM-driven web actions; you pay only LLM tokens |
| LLM | Configurable via env (`LLM_MODEL`, `LLM_API_KEY`) | Start with one provider (e.g. `gpt-4o` or another hosted model); pass a LangChain chat model into Browser-Use |
| Nutrition data | USDA FoodData Central API (free key) | Deterministic macro source; responses cached in SQLite |
| Database | SQLite via `aiosqlite` | Every table carries `user_id`; single-user mode hardcodes `user_default` |
| Frontend | Next.js 14 (App Router) | One chat page; websocket client |
| Config | `pydantic-settings`, `.env` file | |
| Tests | `pytest` | Unit tests for validator/consolidator; manual E2E script |

## 3. Architecture

```
┌─────────────────────────────┐
│  Next.js chat UI            │  chat, progress feed, HITL prompts,
│  (1 page + 4 components)    │  cart review panel
└──────────────┬──────────────┘
               │ websocket (events) + REST
┌──────────────▼──────────────┐
│  FastAPI                    │  POST /api/runs · GET /api/runs/{id}
│                             │  WS  /api/runs/{id}/events
└──────────────┬──────────────┘
               │
┌──────────────▼──────────────┐
│  LangGraph orchestrator     │
│  nodes: intake → plan →     │
│  validate ⇄ repair (≤3) →   │
│  consolidate → shop → report│
└───┬───────────────┬─────────┘
    │               │
┌───▼─────┐   ┌─────▼─────────────────────────┐
│ SQLite  │   │ Shopper (Browser-Use agent)   │
│ users,  │   │  + Playwright session manager │
│ runs,   │   │  persistent profile:          │
│ usda_   │   │  profiles/<user_id>/          │
│ cache,  │   └───────────────┬───────────────┘
│ hitl_   │                   │ real Chromium
│ events  │           ┌───────▼────────┐
└─────────┘           │ instacart.com  │
                      └────────────────┘
┌────────────────┐
│ USDA FoodData  │  deterministic macro lookup (validator only)
│ Central API    │
└────────────────┘
```

**Design rules:**
1. **LLM never does arithmetic.** All macro math is deterministic Python over USDA data.
2. **Deterministic where possible.** Login/session handling and cart read-back are plain
   Playwright scripts. The LLM agent is used only for the fuzzy part: searching products
   and choosing substitutes on a live, changing website.
3. **The agent never pays.** There is no checkout code path. The run's terminal state is
   `cart_ready`.
4. **Every external pause is resumable.** CAPTCHA / email-code / approval pauses keep the
   same browser session alive; the user response resumes it. Never restart the browser
   to handle a pause.

## 4. Repository layout (exact)

```
mealcart-agent/
├── .env.example
├── README.md
├── requirements.txt
├── backend/
│   └── app/
│       ├── __init__.py
│       ├── main.py                 # FastAPI app factory, router wiring
│       ├── config.py               # pydantic-settings Settings
│       ├── db.py                   # sqlite connect, init_db(), helpers
│       ├── models.py               # all Pydantic models (§5)
│       ├── api/
│       │   ├── __init__.py
│       │   ├── runs.py             # POST /api/runs, GET /api/runs/{id}
│       │   └── ws.py               # websocket endpoint /api/runs/{id}/events
│       ├── graph/
│       │   ├── __init__.py
│       │   ├── state.py            # RunState (extends MessagesState or TypedDict)
│       │   ├── nodes.py            # node functions (§7)
│       │   └── graph.py            # StateGraph wiring (§7)
│       ├── nutrition/
│       │   ├── __init__.py
│       │   ├── usda.py             # USDA client + SQLite cache (§8)
│       │   └── validator.py        # macro computation + gates (§8)
│       ├── planner/
│       │   ├── __init__.py
│       │   ├── prompts.py          # planner + repair prompt templates (§9)
│       │   └── planner.py          # structured-output LLM call (§9)
│       ├── shopping/
│       │   ├── __init__.py
│       │   ├── consolidator.py     # meal plan → grocery list (§10)
│       │   ├── substitutions.yaml  # substitution table (§10)
│       │   ├── session.py          # Playwright persistent-profile manager (§11)
│       │   └── shopper.py          # Browser-Use agent wrapper (§12)
│       └── hitl/
│           ├── __init__.py
│           └── events.py           # event schemas + emit helpers (§13)
├── frontend/
│   ├── package.json
│   ├── app/
│   │   ├── page.tsx                # chat page
│   │   └── layout.tsx
│   ├── components/
│   │   ├── ChatPanel.tsx
│   │   ├── ProgressFeed.tsx
│   │   ├── HitlPrompt.tsx          # code input / approval buttons
│   │   └── CartReview.tsx
│   └── lib/
│       └── ws.ts                   # websocket client + types
└── tests/
    ├── test_validator.py
    ├── test_consolidator.py
    └── test_usda_cache.py
```

## 5. Data models (`backend/app/models.py`)

All models are Pydantic v2 `BaseModel`. Macro targets are taken **as given** —
no requirement validation (per product decision).

```python
from pydantic import BaseModel, Field
from typing import Literal

class MacroTargets(BaseModel):
    calories: float            # kcal/day, e.g. 2500
    protein_g: float           # e.g. 140
    carbs_g: float             # e.g. 270
    fat_g: float               # e.g. 75

class MealSpec(BaseModel):
    user_id: str = "user_default"
    days: int = 7
    macros: MacroTargets
    cuisine: str = "Indian"
    budget_weekly_usd: float = 50.0
    zip_code: str                              # e.g. "61801" — drives store choice
    include_snacks: bool = True
    allergies: list[str] = []                  # hard blocklist, enforced deterministically
    dislikes: list[str] = []
    protein_source: str = "chicken"            # e.g. chicken-only

class Ingredient(BaseModel):
    name: str                  # canonical name, e.g. "chicken_thigh_boneless_skinless"
    grams: float               # quantity in this meal
    preparation: str = ""      # e.g. "diced", "cooked"

class Meal(BaseModel):
    slot: Literal["breakfast","mid_morning","lunch","evening","dinner"]
    title: str                 # e.g. "Chicken curry"
    ingredients: list[Ingredient]
    notes: str = ""

class DayPlan(BaseModel):
    day: str                   # "Monday" ... "Sunday"
    meals: list[Meal]

class MealPlan(BaseModel):
    days: list[DayPlan]        # length == MealSpec.days

class GroceryItem(BaseModel):
    name: str                  # canonical ingredient name
    total_grams: float         # summed across the week
    purchase_qty: str          # human/buyable amount, e.g. "4 lb"
    substitutes: list[str] = []# canonical names, from substitutions.yaml

class GroceryList(BaseModel):
    items: list[GroceryItem]

class CartLine(BaseModel):
    product_name: str          # as shown on Instacart
    quantity: str
    unit_price_usd: float
    line_total_usd: float
    substituted_for: str | None = None   # canonical name it replaced, if any

class CartReport(BaseModel):
    store: str
    item_count: int
    lines: list[CartLine]
    subtotal_usd: float
    currency: str = "USD"
    notes: list[str] = []      # substitutions, unavailable items

class ValidationResult(BaseModel):
    passed: bool
    # per-day computed macros and deltas vs target, e.g.
    # {"Monday": {"calories": 2480, "protein_g": 138, ...}, ...}
    per_day: dict[str, dict[str, float]]
    # human-readable repair hints, e.g. "Wednesday: protein 22g over target — reduce chicken by ~90g"
    deltas: list[str] = []
```

## 6. Database (`backend/app/db.py`)

SQLite. Every domain table carries `user_id` (single-user mode always uses
`'user_default'`; this is the entire multi-user migration surface).

```sql
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,              -- running | awaiting_hitl | cart_ready | failed
  meal_spec_json TEXT NOT NULL,
  meal_plan_json TEXT,               -- filled after planner
  grocery_list_json TEXT,            -- filled after consolidator
  cart_report_json TEXT,             -- filled after shopper
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usda_cache (
  query TEXT PRIMARY KEY,            -- normalized ingredient name
  payload_json TEXT NOT NULL,        -- macros per 100g + source food id
  cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hitl_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- captcha | email_code | substitution_approval | plan_approval
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,              -- open | resolved
  created_at TEXT NOT NULL
);
```

`init_db()` runs these statements on startup. Keep access behind small async
helpers (`save_run`, `get_run`, `log_hitl_event`, …) in `db.py` — no ORM.

## 7. LangGraph orchestration (`backend/app/graph/`)

`state.py` — `RunState` is a `TypedDict`:

```python
from typing import TypedDict, Optional
from app.models import MealSpec, MealPlan, GroceryList, CartReport, ValidationResult

class RunState(TypedDict):
    run_id: str
    spec: MealSpec
    meal_plan: Optional[MealPlan]
    validation: Optional[ValidationResult]
    repair_attempts: int
    grocery_list: Optional[GroceryList]
    cart_report: Optional[CartReport]
    error: Optional[str]
```

`graph.py` — node wiring:

```
intake → plan → validate → [passed?]
                            ├─ yes → consolidate → shop → report → END
                            └─ no  → repair → validate   (repair_attempts < 3)
                                     └─ attempts exhausted → END with error
                                              "plan_failed_validation; deltas attached"
```

`nodes.py` — one async function per node; each node:
1. loads what it needs from state,
2. calls its module (`planner.generate`, `validator.check`, `consolidator.build`,
   `shopper.fill_cart`),
3. writes results back to state **and** persists to the `runs` row,
4. emits a websocket progress event via `hitl/events.py`.

**HITL points** (LangGraph `interrupt()`):
- After `consolidate`, before `shop`: `interrupt({"kind": "plan_approval", "grocery_list": ...})`
  — user confirms the list (or edits it) before any browser work starts.
- Inside `shop` (see §12): `interrupt({"kind": "email_code"|"captcha_decision", ...})`.

Interrupt payloads must be JSON-serializable; the websocket layer (§13) forwards
them to the frontend and feeds the user's response back into `graph.ainvoke`
resumption.

## 8. Meal planner (`backend/app/planner/`)

`planner.py::generate(spec: MealSpec) -> MealPlan`:
- Calls the LLM with **structured output** bound to the `MealPlan` schema
  (e.g. LangChain `with_structured_output(MealPlan)` or the provider's native
  JSON-schema mode). Temperature 0.3.
- `prompts.py` holds `PLANNER_SYSTEM` / `PLANNER_USER` templates. The user
  template injects: days, per-day macro targets, cuisine, protein source,
  allergies (as hard exclusions), dislikes, budget hint, and this instruction:
  *"Use common grocery ingredients with USDA entries. Give every ingredient a
  canonical snake_case name and a gram weight. Do not invent brand products."*
- Repair: `planner.repair(spec, plan, deltas: list[str]) -> MealPlan` re-sends
  the previous plan plus the validator's delta hints and asks for a corrected
  plan addressing each hint. Same schema, temperature 0.2.

## 9. Macro validator (`backend/app/nutrition/`)

Deterministic. The LLM never estimates calories.

`usda.py`:
- `get_macros_per_100g(canonical_name: str) -> dict` — first checks `usda_cache`,
  else calls `GET https://api.nal.usda.gov/fdc/v1/foods/search` with
  `api_key` (from `.env`, free at api.data.gov), `query=<canonical_name>`,
  `dataType=["SR Legacy","Foundation"]`, `pageSize=3`.
- Picks the first result; stores `{calories, protein_g, carbs_g, fat_g}` per 100g
  plus the FDC food id in `usda_cache`. On zero results, raises
  `UnknownIngredientError(name)` — the planner must rename/replace the ingredient
  (handled as a repair delta).
- Name normalization: lowercase, spaces→underscores, strip preparation words via
  a small `ALIAS` map in `usda.py` (e.g. `"chicken_thigh"` →
  `"chicken, broilers or fryers, thigh, meat only, raw"` is overkill — the search
  query `"chicken thigh raw"` is sufficient; keep aliases only for common misses).

`validator.py::check(plan: MealPlan, targets: MacroTargets) -> ValidationResult`:
1. For each day, for each meal, for each ingredient:
   `contrib = grams/100 × macros_per_100g`.
2. Sum per day → `per_day[day] = {calories, protein_g, carbs_g, fat_g}`.
3. Gates: `|calories − target| / target ≤ 0.05`; each macro `|actual − target| ≤ 10g`.
4. `passed = all days pass all gates`.
5. On failure, build `deltas`: one actionable string per violation, naming the
   biggest contributor, e.g.
   `"Wednesday: protein 22g over target (chicken_thigh_boneless_skinless is the
   largest contributor) — reduce it by ~90g or swap 60g for paneer."`
   Compute the suggested gram change as `delta_g / (macro_per_100g/100)` and
   round to the nearest 10g. Keep hints mechanical, not clever.

`tests/test_validator.py` must cover: exact-pass fixture, single-day failure,
unknown-ingredient error path, and delta-math correctness.

## 10. Grocery consolidator (`backend/app/shopping/consolidator.py`)

Deterministic. `build(plan: MealPlan) -> GroceryList`:
1. Walk all days/meals/ingredients; sum grams by canonical name.
2. Drop anything in `spec.allergies` (defense in depth — planner was told, but
   enforce here; if a dropped ingredient breaks a meal, mark the plan invalid
   and return to the repair loop with a delta).
3. Convert grams → buyable `purchase_qty` with a `UNIT_ROUNDING` table, e.g.
   chicken → nearest 0.5 lb, rice → nearest 1 lb bag, eggs → nearest dozen,
   spices → "1 pack". Keep it simple: a dict of `canonical_name → (unit, step)`.
4. Attach `substitutes` per item from `substitutions.yaml` (canonical names only).

`substitutions.yaml` — each entry carries macros so the validator can re-check
after a swap:

```yaml
toor_dal:
  substitutes:
    - name: masoor_dal
      macros_per_100g: {calories: 352, protein_g: 24.6, carbs_g: 63.1, fat_g: 1.1}
      note: "cooks faster, no soaking"
    - name: yellow_split_peas
      macros_per_100g: {calories: 341, protein_g: 24.5, carbs_g: 60.3, fat_g: 1.2}
atta_whole_wheat:
  substitutes:
    - name: whole_wheat_flour
      macros_per_100g: {calories: 340, protein_g: 13.2, carbs_g: 72.0, fat_g: 2.5}
chicken_thigh_boneless_skinless:
  substitutes:
    - name: chicken_breast_boneless_skinless
      macros_per_100g: {calories: 120, protein_g: 22.5, carbs_g: 0.0, fat_g: 2.6}
green_chili:
  substitutes:
    - name: jalapeno
      macros_per_100g: {calories: 29, protein_g: 0.9, carbs_g: 6.5, fat_g: 0.4}
```

Rule: a substitution is **auto-applied** only if re-running the validator on the
affected days still passes the gates; otherwise emit a `substitution_approval`
HITL event with old vs new macros and wait.

## 11. Browser session manager (`backend/app/shopping/session.py`)

Playwright only — no LLM here. Purpose: persistent login so most runs skip sign-in.

```python
PROFILE_ROOT = "profiles"          # gitignored

async def get_context(user_id: str):
    """Return a persistent Playwright context for the user, creating it on first use."""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        f"{PROFILE_ROOT}/{user_id}",
        headless=False,            # headful: CAPTCHAs are unsolvable headless
        viewport={"width": 1366, "height": 900},
    )
    return pw, ctx

async def ensure_logged_in(ctx, page_url="https://www.instacart.com/") -> bool:
    """True if the profile already holds a valid session (heuristic: account menu present)."""
```

- First ever run: launch headful, user logs in manually once (or via HITL), close —
  the profile directory now holds the session.
- Every run starts with `ensure_logged_in`; if false → HITL `login_required` event.
- Never store passwords in the repo. Session cookies live only in `profiles/`
  (gitignored, `chmod 700`).

## 12. Shopping agent (`backend/app/shopping/shopper.py`)

Hybrid design: **deterministic Playwright for structure, Browser-Use agent for the
fuzzy part** (searching products, picking substitutes on a live site).

`fill_cart(spec, grocery_list, user_id, emit) -> CartReport`:

**Phase A — deterministic (Playwright, `session.py`):**
1. `ctx = get_context(user_id)`; if not logged in → HITL login flow, then continue.
2. Navigate to instacart.com, set delivery ZIP to `spec.zip_code`.
3. Select store: prefer a well-rated general grocer serving the ZIP
   (instruction text; the agent below can decide, but log the choice).

**Phase B — Browser-Use agent (the fuzzy part):**
Build one `browser-use` Agent sharing the *same* Playwright context:

```python
from browser_use import Agent, Browser
# NOTE: verify constructor names against the installed browser-use version.
browser = Browser(playwright_context=ctx)   # reuse session from Phase A
agent = Agent(
    task=SHOPPING_TASK_TEMPLATE.format(items=rendered_list, zip=spec.zip_code),
    llm=make_llm(),                          # LangChain chat model from env
    browser=browser,
    max_steps=80,
)
```

`SHOPPING_TASK_TEMPLATE` (adapt the exact wording; keep the constraints):
> "Add these grocery items to the Instacart cart, one search per item, choosing
> the best-value regular option (store brand is fine). Quantities: {items}.
> If an exact item is unavailable, pick the closest substitute and remember what
> it replaced. Do NOT apply coupons. Do NOT start checkout. Do NOT place the
> order. When every item is in the cart, open the cart and output ONLY this JSON:
> {\"store\": ..., \"lines\": [{\"product_name\":..., \"quantity\":..., 
> \"unit_price_usd\":..., \"line_total_usd\":..., \"substituted_for\":...|null}],
> \"subtotal_usd\":..., \"notes\": [...]}."

- The agent's `final_result()` is parsed as JSON → validated against `CartLine`/
  `CartReport`. Parse failure → one retry asking the agent to re-emit JSON; then
  fail the run with the raw text attached for debugging.
- **Mid-run HITL:** if the page shows a CAPTCHA or verification-code dialog,
  pause the agent (do not close the browser), emit `hitl.required`, and resume
  the same agent/browser after the user's response. Implement with a step
  callback if the installed version supports it; otherwise poll page state
  between agent restarts with identical context.

**Phase C — deterministic verification (Playwright):**
Re-read the cart independently of the agent's report (open cart, scrape lines).
If Phase C disagrees with the agent's JSON on item count or subtotal by more
than rounding, mark the run `failed` with both versions attached — never report
an unverified cart.

Post-condition: `CartReport` saved to the run row, `cart_ready` event emitted.

## 13. HITL protocol (websocket)

`WS /api/runs/{run_id}/events` — bidirectional JSON messages.

Server → client:
```json
{"type": "run.progress", "run_id": "...", "node": "shop", "message": "Added chicken thighs (4 lb) — $19.16"}
{"type": "hitl.required", "run_id": "...", "kind": "email_code",
 "prompt": "Instacart emailed a 6-digit code to s***@gmail.com. Reply with the code.",
 "expires_in_s": 600}
{"type": "hitl.required", "run_id": "...", "kind": "captcha_decision",
 "prompt": "Instacart shows an image CAPTCHA. Allow the agent to attempt it?"}
{"type": "hitl.required", "run_id": "...", "kind": "substitution_approval",
 "prompt": "No toor dal at this store. Swap 2 lb toor dal → 2 lb masoor dal? Day macros stay within tolerance."}
{"type": "hitl.required", "run_id": "...", "kind": "plan_approval",
 "prompt": "Grocery list ready (18 items, est. $47). Approve to start Instacart?"}
{"type": "cart.ready", "run_id": "...", "report": { /* CartReport */ }}
{"type": "run.failed", "run_id": "...", "error": "plan_failed_validation", "detail": ["...deltas..."]}
```

Client → server:
```json
{"type": "hitl.response", "run_id": "...", "kind": "email_code", "value": "310797"}
{"type": "hitl.response", "run_id": "...", "kind": "captcha_decision", "value": "allow"}
{"type": "hitl.response", "run_id": "...", "kind": "plan_approval", "value": "approved"}
```

Rules: every `hitl.required` is persisted to `hitl_events` as `open` and marked
`resolved` on response. Responses are accepted only for the currently-open event
of that run (ignore stale ones). Secrets (codes) are never logged — log the
*kind*, not the value.

## 14. REST API contracts (`backend/app/api/runs.py`)

`POST /api/runs` — body is a `MealSpec` JSON (see §5). Response:
```json
{"run_id": "run_9f3a...", "status": "running",
 "ws_url": "/api/runs/run_9f3a.../events"}
```
The endpoint creates the DB row, then launches the LangGraph run as a background
asyncio task. It returns immediately; all progress arrives over the websocket.

`GET /api/runs/{run_id}` — returns the full run row (spec, plan, list, cart
report if present, status). Used by the frontend on reconnect.

## 15. Frontend (`frontend/`, Next.js 14 App Router)

Keep it minimal but functional — one page, four components:
- `app/page.tsx` — chat layout; on submit, POSTs a `MealSpec` built from a small
  form (number inputs for macros/budget/days, text for cuisine/ZIP, checkboxes
  for snacks, textarea for allergies/dislikes). A free-text "describe it" box is
  a stretch goal — the form is the v1 contract.
- `components/ChatPanel.tsx` — message list; renders progress events inline.
- `components/ProgressFeed.tsx` — step-by-step run timeline from `run.progress`.
- `components/HitlPrompt.tsx` — renders per `kind`: text input for `email_code`,
  Allow/Deny buttons for `captcha_decision`, Approve/Edit for `plan_approval`
  and `substitution_approval`.
- `components/CartReview.tsx` — table of `CartReport.lines` + subtotal +
  substitution notes + "Open Instacart to check out" hint (deep link to the
  store cart page; the agent never checks out).
- `lib/ws.ts` — typed websocket client with reconnect + the message types from §13.

## 16. Environment & setup (exact)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -U pip
```

`requirements.txt` (resolve at install time; verify imports after install):
```
fastapi>=0.110
uvicorn[standard]>=0.29
langgraph>=0.2
langchain-openai>=0.1          # or another provider; pick ONE provider
browser-use>=0.2               # verify Agent/Browser constructor names in installed version
playwright>=1.44
pydantic>=2.7
pydantic-settings>=2.3
aiosqlite>=0.19
httpx>=0.27
pytest>=8.0
```
```bash
pip install -r requirements.txt
playwright install chromium
```

`.env.example`:
```
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o
USDA_API_KEY=        # free at https://api.data.gov/signup/
PROFILE_ROOT=profiles
DATABASE_URL=sqlite+aiosqlite:///./mealcart.db
SINGLE_USER_ID=user_default
```

## 17. Build phases (in order — each phase is demoable)

**Phase 0 — Scaffold.** Repo layout (§4), `requirements.txt`, `.env.example`,
`config.py`, `db.py` with `init_db()`. *Done when:* `pytest` collects, FastAPI
serves `GET /health`.

**Phase 1 — Nutrition core (no LLM, no browser).** `usda.py` + cache,
`validator.py`, `tests/test_validator.py` with fixtures. *Done when:* a
hand-written 1-day plan validates green and a broken one returns correct deltas.

**Phase 2 — Planner + repair loop.** `prompts.py`, `planner.py`,
LangGraph with nodes `plan → validate → repair(≤3)`. Test with a mocked USDA
client. *Done when:* an intentionally bad plan gets repaired to passing within
3 rounds (log the rounds for your report).

**Phase 3 — Consolidator + substitutions.** `consolidator.py`,
`substitutions.yaml`, `tests/test_consolidator.py`. *Done when:* a 7-day plan
collapses to a deduplicated list with buyable quantities and substitute hints.

**Phase 4 — Session manager.** `session.py`; manual first login; prove
`ensure_logged_in()` returns true on second launch without interaction.
*Done when:* two consecutive launches, second needs no login.

**Phase 5 — Shopper.** `shopper.py` Phase B+C against the real site with a
5-item list. Keep `max_steps` low; print the raw agent transcript for tuning.
*Done when:* 5/5 items land in cart and Phase C verification matches.

**Phase 6 — Orchestration + API.** `graph.py`, `nodes.py`, `api/runs.py`,
`api/ws.py`, `hitl/events.py`, HITL pause/resume tested with a fake
`email_code` event. *Done when:* a full run completes end-to-end via API with
one simulated HITL pause.

**Phase 7 — Frontend.** The five files in §15, wired to the websocket protocol.
*Done when:* you can drive a whole run from the browser, paste a code, and see
the cart report render.

**Phase 8 — Hardening & report.** Retry/backoff on USDA, token budget caps on
the agent (`max_steps`, per-run LLM spend log), `README.md` with the demo
script, and a risks section (see §19) in your project report.

## 18. Testing

- **Unit** (`pytest`): validator gates + delta math; consolidator dedup/rounding;
  USDA cache hit/miss.
- **Contract**: every HITL event round-trips through `hitl/events.py` schemas.
- **Integration**: LangGraph run with mocked planner + mocked shopper asserts
  state transitions and DB persistence.
- **E2E (manual, for the demo)**: the §17 Phase 6/7 checklist. Record one clean
  run — screen-record it for the class demo; live demos of CAPTCHAs are brave.

## 19. Risks & mitigations

| Risk | Mitigation |
|---|---|
| CAPTCHA / 2FA mid-run | HITL pause/resume (§12–13); persistent profile means most runs skip login |
| Instacart layout changes | Agent-based shopping (not CSS selectors) for the fuzzy part; deterministic scripts only for stable flows |
| ToS / account flags | Personal educational use; human-speed actions; never parallelize against one account; disclose in report |
| LLM cost per run | `max_steps` cap; token/log per run; planner temperature low; cache USDA |
| Hallucinated nutrition | Validator is deterministic over USDA data; LLM never does macro math (§9) |
| Session expiry | `ensure_logged_in` check each run → HITL re-login, never silent failure |
| Multi-user later | `user_id` on all tables + `profiles/<user_id>/` from day one (§6, §11) |

## 20. Definition of done

A reviewer can: `git clone`, follow `README.md` setup, open the chat UI, submit
a `MealSpec`, watch the plan validate (≤3 repair rounds visible), approve the
grocery list, watch the browser agent fill the Instacart cart (handling one
simulated HITL pause), and see a verified `CartReport` with substitutions noted
— without the agent ever checking out.
