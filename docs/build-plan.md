# MealCart Agent: Revised Build Plan

> Supersedes `original-plan.md`. Every gap from `plan-review.md` is fixed here.
> Build the phases in order (§16). Each one can be demoed on its own.

## 1. Goal

A single-user local web app:
1. It takes your nutrition targets (calories + protein) and cooking constraints.
2. It writes a 7-day meal plan and checks it in Python against USDA data.
3. It fills your Instacart cart with Playwright, using one LLM call per item to choose the product.
4. **It stops at a filled, verified cart. You check out by hand.**

## 2. Pipeline

```
Form or natural-language request
  → intake (NL → MealSpec, warn-only checks)
  → plan (LLM picks meals + ingredients, raw grams)
  → solve portions (scipy, hits calories + protein per day)
  → validate (deterministic gates) ──fail──→ repair (LLM, failing days only, ≤3) ─┐
        │ pass                                                                   │
        ▼                                         ◄──────────────────────────────┘
  → consolidate (grocery list, pantry removed, packs sized)
  → [interrupt] plan_approval (you see plan + list, approve or edit)
  → shop (Playwright + per-item LLM pick; pauses via SessionRegistry)
  → verify (cart scraped and compared with the GroceryList)
  → report: cart_ready | needs_attention
```

## 3. Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11 |
| API | FastAPI + uvicorn (run **without** `--reload` while shopping) |
| Orchestration | LangGraph + `AsyncSqliteSaver` checkpointer, `thread_id = run_id` |
| Browser | Playwright, persistent profile, real Chrome (`channel="chrome"`), headful |
| LLM | **Free**, via any OpenAI-compatible endpoint (`openai` package + `LLM_BASE_URL`). Default: Google Gemini free tier. Offline alternative: Ollama on the Mac (`qwen3:8b`). |
| Nutrition | USDA FoodData Central (free key), cached in SQLite |
| Portion solver | `scipy.optimize` (bounded least squares) |
| DB | SQLite via `aiosqlite`, `user_id` on every domain table |
| Frontend | Next.js 14 App Router, one page |
| Tests | `pytest` + `pytest-asyncio` |

Browser-Use is **not** used. A computer-use model is a later fallback for single items whose selectors fail (Phase 8). The Instacart Developer Platform "products link" is an optional fallback for the demo (Phase 8).

## 4. Design rules

1. **The LLM never does arithmetic.** Macros, portion grams, pack counts and budget totals are all computed in Python.
2. **Python checks everything the LLM is told.** Allergies, dislikes, restrictions, prep time and equipment are all checked deterministically after planning.
3. **The agent can never pay.** No checkout code path exists. A route blocker rejects checkout and order URLs. The code only clicks "Add" buttons. Setup removes the saved payment method.
4. **Pauses never restart the browser.** The browser lives in a process-level `SessionRegistry`.
5. **Secrets are never stored.** Codes and passwords live in memory only, never in the DB, logs or checkpoints.
6. **The agent never solves CAPTCHAs.** You do, in the chat or the LiveView.

## 5. Repository layout

```
mealcart-agent/
├── .env.example  .gitignore  README.md  requirements.txt  pytest.ini
├── docs/                         original-plan.md, plan-review.md, build-plan.md
├── backend/app/
│   ├── main.py  config.py  db.py  models.py
│   ├── api/        runs.py  ws.py
│   ├── graph/      state.py  nodes.py  graph.py  registry.py
│   ├── intake/     intake.py                     # NL → MealSpec, feasibility warnings
│   ├── nutrition/  usda.py  validator.py  solver.py  maps.py   # ALIAS / ALLERGEN / RESTRICTION maps
│   ├── planner/    prompts.py  planner.py  llm.py              # llm.py: OpenAI-compatible client + call log
│   ├── shopping/   consolidator.py  packs.py  substitutions.yaml
│   │               session.py  guard.py  shopper.py  verify.py
│   └── hitl/       events.py
├── frontend/  app/page.tsx  components/{ChatPanel,ProgressFeed,HitlPrompt,PlanView,CartReview,LiveView}.tsx  lib/ws.ts
└── tests/
```

## 6. Data models (`backend/app/models.py`)

```python
class MacroTargets(BaseModel):
    calories: float            # kcal/day
    protein_g: float           # g/day
    # carbs and fat are NOT targets. They are computed and shown for information only.

EffortLevel  = Literal["low", "low_moderate", "moderate", "high"]
CookingSkill = Literal["basic", "intermediate", "advanced"]
Slot         = Literal["breakfast", "mid_morning", "lunch", "evening", "dinner"]

class MealSpec(BaseModel):
    user_id: str = "user_default"
    days: int = 7                                  # 1..7
    macros: MacroTargets
    cuisines: list[str] = []                       # empty = any
    budget_weekly_usd: float | None = None
    zip_code: str
    include_snacks: bool = True                    # False forbids mid_morning/evening
    allergies: list[str] = []                      # hard block, via ALLERGEN_MAP
    dietary_restrictions: list[str] = []           # hard block, via RESTRICTION_MAP
    dislikes: list[str] = []                       # hard block, by name match
    protein_source: str | None = None              # e.g. "chicken"
    max_prep_minutes: int = 30                     # per meal
    effort_level: EffortLevel = "low_moderate"
    cooking_skill: CookingSkill = "basic"
    equipment: list[str] = ["stove", "microwave", "oven", "rice_cooker"]
    pantry: list[str] = []                         # canonical names you already have; never bought

class Ingredient(BaseModel):
    name: str                  # canonical snake_case
    grams: float               # RAW / DRY weight, always
    preparation: str = ""

class Meal(BaseModel):
    slot: Slot
    title: str
    ingredients: list[Ingredient]
    steps: list[str] = []
    prep_minutes: int
    equipment_used: list[str] = []
    notes: str = ""

class DayPlan(BaseModel):  day: str; meals: list[Meal]
class MealPlan(BaseModel): days: list[DayPlan]

class GroceryItem(BaseModel):
    name: str
    total_grams: float
    purchase_qty: str          # display, e.g. "2 × 1.5 lb"
    substitutes: list[str] = []
    est_price_usd: float | None = None   # from last known cart prices

class GroceryList(BaseModel):
    items: list[GroceryItem]
    est_total_usd: float | None = None

class CartLine(BaseModel):
    product_name: str
    pack_size: str             # as scraped, e.g. "1.5 lb"
    pack_grams: float          # parsed by packs.py
    packs: int
    unit_price_usd: float
    line_total_usd: float
    grocery_item: str          # canonical name this line covers
    substituted_for: str | None = None

class CoverageRow(BaseModel):
    item: str; grams_needed: float; grams_in_cart: float; covered: bool

class CartReport(BaseModel):
    store: str
    lines: list[CartLine]
    coverage: list[CoverageRow]
    subtotal_usd: float
    over_budget_by_usd: float | None = None
    notes: list[str] = []      # substitutions, unavailable items, address restore

class ValidationResult(BaseModel):
    passed: bool
    per_day: dict[str, dict[str, float]]   # calories, protein_g, carbs_g, fat_g (last two informational)
    deltas: list[str] = []                 # repair hints

RunStatus = Literal["running", "awaiting_hitl", "cart_ready", "needs_attention", "failed"]
HitlKind  = Literal["plan_approval", "login_email", "login_password", "email_code",
                    "captcha", "substitution_approval", "cart_clear_approval"]
```

## 7. Database (`backend/app/db.py`)

`DATABASE_PATH` is a plain file path. Tables:
- `users(id, created_at)`
- `runs(id, user_id, status, meal_spec_json, meal_plan_json, grocery_list_json, cart_report_json, error, llm_cost_usd, created_at, updated_at)`
- `usda_cache(query PK, payload_json, cached_at)`
- `hitl_events(id, run_id, seq, kind, payload_json, status open|resolved, created_at, resolved_at)`. **The response value is never stored.**
- `llm_calls(id, run_id, purpose, model, input_tokens, output_tokens, cost_usd, created_at)`
- `price_history(user_id, item, product_name, pack_grams, unit_price_usd, seen_at)`, for budget estimates

There is no ORM. Small async helpers handle access. LangGraph checkpoints go in a separate `checkpoints.db`.

## 8. Intake (`intake/intake.py`)

- Natural language → `MealSpec` with one structured LLM call. The form posts a `MealSpec` directly.
- Warn-only checks, shown in chat, never rejected:
  - `protein_g × 4 > 0.6 × calories` → "protein is X% of calories; plans will be very lean-meat heavy"
  - `days × 2 slots < 1` and similar sanity checks
- Effort/skill → prompt text (see §10).

## 9. Nutrition (`nutrition/`)

**usda.py.** `get_macros_per_100g(name)`:
1. Normalize the name, then apply `ALIAS` (about 30 hand-checked staples, e.g. `basmati_rice` → `"rice white long-grain raw"`).
2. Check the cache.
3. Search FDC with `query="<name> raw"`, `dataType=["Foundation","SR Legacy"]`, `pageSize=10`. Prefer descriptions containing "raw" and avoid "cooked", "fried" and "breaded". Otherwise take the first result.
4. Energy: nutrient 1008 (kcal), else 2047, else 2048. Protein 1003, fat 1004, carbs 1005.
5. Retry with backoff on 429 or 5xx. On zero hits, raise `UnknownIngredientError` → becomes a repair hint.

**maps.py.**
- `ALLERGEN_MAP`: dairy → milk, ghee, butter, paneer, curd, yogurt, cheese, cream. Also tree_nuts, peanuts, gluten (atta, wheat, maida, bread, pasta…), egg, soy, shellfish, fish, sesame.
- `RESTRICTION_MAP`: vegetarian, vegan, pescatarian, halal (no pork), no_beef, no_pork.
- Dislikes match by substring (`mushroom` matches `button_mushrooms`), plus a few synonyms (mayonnaise ↔ mayo).

**solver.py.** For each day, choose a gram scale for each ingredient within bounds so the day hits the calorie and protein targets:
- Bounds: `[0.5×, 2×]` of the LLM's grams, clamped by per-category limits (oil ≤ 30 g per meal, spices unchanged).
- Bounded least squares over `[kcal; protein]` rows, weighting calories by 1/target and protein by 1/target.
- Rounds to 5 g and returns the new plan and whether the targets were reached.

**validator.py.** `check(plan, spec) -> ValidationResult`. The gates:
1. `|kcal − target| / target ≤ 0.05`
2. `|protein − target| ≤ 10 g`
3. No ingredient hits allergies, restrictions or dislikes
4. `prep_minutes ≤ max_prep_minutes`
5. `equipment_used ⊆ spec.equipment`
6. When skill is basic, `len(steps) ≤ 8`
7. When `include_snacks=False`, no snack slots

Every failure produces one mechanical hint naming the day, meal and biggest contributor, e.g. `"Wed dinner: uses 'mushrooms' (disliked) — replace it"` or `"Tue: protein 18 g under — add ~80 g chicken_breast"`. Gram suggestion = `delta / (per_100g/100)`, rounded to 10 g. Carbs and fat are reported but never gated.

## 10. Planner (`planner/`)

- `llm.py`: `AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)`.
  - Structured output: `response_format={"type": "json_schema", ...}` built from the Pydantic model. If an endpoint rejects that, fall back to `{"type": "json_object"}` with the schema in the prompt.
  - Every reply is validated with Pydantic. On a parse error, retry once with the error message attached.
  - Retry with backoff on 429, because free tiers are rate-limited.
  - Every call logs tokens to `llm_calls`. `cost_usd` is 0 on free tiers but kept, so a paid model can be swapped in later.
- **Few calls per run.** Gemini's free tier caps each model at about 20 requests a day. `generate` plans `LLM_DAYS_PER_CALL` days per call; the default 7 means one call per week. Set it to 1 for small local models. `repair` fixes all failing days in **one** call per round. A typical run makes 1 to 4 planning calls. A used-up quota ends the run with `llm_quota_exceeded` and a "try again in …" message.
- Variety gate: no meal title more than twice a week (leftovers allowed).
- `generate(spec)`, temperature 0.3. The prompt includes:
  - days, the calorie and protein targets, cuisines, protein source, budget hint
  - **hard exclusions**: allergies, restrictions, dislikes
  - prep limit, equipment list, effort/skill guidance (basic skill → one-pot, sheet-pan, rice-cooker or microwave meals, no deep-frying, ≤8 steps)
  - "All grams are RAW/DRY weight. Always list cooking oil or ghee explicitly. Use common grocery ingredients with USDA entries; snake_case names; no brands. Reuse ingredients across the week to limit the grocery list."
- Then: solver → validator.
- Every call uses the same `llm.py` path, so changing provider is a `.env` change.
- `repair(spec, plan, failing_days, deltas)`, temperature 0.2. It resends **only the failing days** plus the hints. The result is merged back in, then solver → validator again. Max 3 rounds. After that the run ends `failed` with the hints attached.

## 11. Consolidator (`shopping/consolidator.py`, `packs.py`)

- Sums grams by canonical name and drops `spec.pantry` items. Allergens were already caught by the validator; one final assert remains as a second safety check.
- `packs.py`:
  - `parse_size("1.5 lb" | "32 oz" | "12 ct" | "each")` → grams, with an `EACH_GRAMS` table (onion 150, tomato 120, egg 50, lemon 60…)
  - `packs_needed = ceil(needed / pack_grams)`
- Before shopping, `purchase_qty` is a target (e.g. "≈ 2.6 lb"). The real pack counts come from the shopper.
- `substitutions.yaml` lists **names only**. Substitute macros always come from USDA, the same source the validator uses.
- `est_price_usd` comes from `price_history` when it is available. `est_total_usd` is shown on the approval card.

## 12. Session + guard (`shopping/session.py`, `guard.py`)

- `launch_persistent_context(profiles/<user_id>, channel="chrome", headless=False)`. A per-user `asyncio.Lock` plus a lockfile means only one run opens a profile at a time. `profiles/` is `chmod 700` and gitignored.
- `guard.install(ctx)`: `ctx.route()` aborts any request or navigation matching `/checkout|/orders/|place_order|/payment`, and logs the attempt.
- `ensure_logged_in(page)`: true if the account menu is present. If not, login goes through the chat:
  1. `login_email` → typed in
  2. `email_code` or `login_password` → typed in, memory only
  3. `captcha`: the chat shows a screenshot. Text CAPTCHAs are answered in the chat. Interactive ones (press-and-hold, drag) are solved in the **LiveView**.
- The ZIP you enter is **checked, not applied**: the cart screen shows the delivery ZIP of your Instacart account, and a mismatch is reported as a warning before items are added. Changing a saved delivery address is deliberately left to you.
- Human-speed actions (300–900 ms of jitter).

## 13. Shopper (`shopping/shopper.py`, `verify.py`)

**Before the run:**
- Read the cart.
- If it isn't empty, pause with `cart_clear_approval`: clear it, or keep it and track only what changes.

**For each GroceryItem, in the same page:**
1. `search(item)` using role/text selectors (`get_by_role("searchbox")`).
2. `scrape_results()`: the top ~8 results as `{index, name, size, price, in_stock}`, with each size parsed to grams.
3. `pick_product()`: one structured LLM call that returns `{index, is_substitute, reason}`. Python computes `packs`.
4. If it's a substitute: look up its USDA macros, re-run the validator on the affected days with the swap applied, and either approve it automatically or pause with `substitution_approval`.
5. **Idempotent add:** read the cart line for this product first, then click "Add" until the quantity equals `packs`. Re-read to confirm. Retry once, then flag the item.
6. Send `run.progress` ("Added chicken thighs 2 × 1.5 lb — $13.98") and record the price in `price_history`.

**More than one store:** stores are ranked by the probe. Items the first store lacks are searched at
the next store, and so on, up to `MAX_STORES` (3), each a separate Instacart cart with its own fee and
minimum. Substitutes are tried only for items no store had. The report groups lines by store, with
per-store subtotals.

**Verify (Phase C, `verify.py`):**
- Scrape the whole cart and map each line to a GroceryItem.
- Build `coverage` (grams needed vs grams in cart).
- Any gap, or any line that isn't on the list → `needs_attention` with the reasons. All covered → `cart_ready`.
- Budget: `over_budget_by_usd` when the subtotal is over budget.
- Restore the delivery address if it was changed.

## 14. Pauses, websocket and API

**Changing the plan before shopping.** At `plan_approval` you can approve, reject, edit the
grocery list, or **swap a meal**: `{"swap": {"day", "slot", "reason"?, "avoid"?}}`.
- One LLM call replaces that meal; the rest of the week is untouched.
- `avoid` ingredients join the run's dislikes.
- Only that day is re-sized, then re-validated (repairs if needed), and the list is rebuilt.
- You approve again. Nothing reaches Instacart until you approve.
- Editing after the cart is filled is not supported: start a new run and choose "clear".

**Two pause mechanisms:**
- **Node boundary:** `plan_approval` uses LangGraph `interrupt()` and resumes with `Command(resume=value)`.
- **Inside `shop`:** `SessionRegistry[run_id] = {ctx, page, pending: Future}`. The shop node `await`s `registry.ask(run_id, kind, prompt)`, and the websocket handler resolves the Future. The browser never closes. Responses never touch the checkpointer.

**Websocket** `WS /api/runs/{id}/events?since=<seq>`. Every server message carries `seq`. On reconnect the client sends `since`, and the server replays from `hitl_events` and an in-memory ring buffer.

Server → client: `run.progress`, `plan.ready` (MealPlan + ValidationResult), `hitl.required {kind, prompt, screenshot_b64?, expires_in_s?}`, `live.frame` (LiveView, ~2 fps JPEG), `cart.ready`, `run.needs_attention`, `run.failed`.

Client → server:
- `hitl.response {kind, value}`. `plan_approval` takes `"approved"` or `{"edit": GroceryList}`; `captcha` takes text or `"done"`.
- `live.input {type: down|move|up|click|key, x, y, key?}`. The server scales the coordinates to the viewport.

Responses are accepted only for the run's currently open event. Values are never logged.

**REST:**
- `POST /api/runs` takes a `MealSpec` or `{"text": "..."}`, returns `{run_id, status, ws_url}`, and starts the background task.
- `GET /api/runs/{id}` returns the row, `open_hitl_event` and the last `seq`.
- `GET /health`.

## 15. Frontend

One page:
- **Form + NL box:** all `MealSpec` fields. Equipment and restrictions are checkboxes.
- **ChatPanel**
- **ProgressFeed**
- **PlanView:** days → meals, with steps, prep time, and per-day kcal/protein (carbs/fat shown in grey)
- **HitlPrompt:** a card per kind
- **LiveView:** screenshot canvas that forwards pointer events
- **CartReview:** lines, coverage table, subtotal vs budget, notes, and an "Open Instacart to check out" link

Pauses trigger a browser `Notification` and a sound, behind a `notify()` interface. `lib/ws.ts` is a typed client with reconnect and `since`.

## 16. Phases

| # | Phase | Done when |
|---|---|---|
| 0 | Scaffold: config, db, models, `/health`, tests | `pytest` green; `curl /health` → ok |
| 1 | Nutrition core: usda, maps, validator | hand-written day passes; broken day returns the right hints; raw/cooked and unknown-ingredient tests pass |
| 2 | Solver + planner + repair (graph: intake→plan→solve→validate⇄repair) | a bad plan reaches passing in ≤3 rounds; rounds and cost logged. **Done:** a live 7-day plan passes in 1 call (`scripts/plan_demo.py`) |
| 3 | Consolidator + packs + pantry + substitutions | a 7-day plan → a deduplicated list with no pantry items; size parsing tests pass. **Done:** the live 7-day plan (103 ingredient lines) → 25 items |
| 4 | Session + guard + chat login | second launch needs no login; guard test blocks a checkout URL |
| 5 | Shopper + verify | a 5-item list run 3 times gives an identical cart; coverage is all green |
| 6 | Graph + API + websocket + registry + checkpointer | a full run via the API with a fake `email_code` pause; launch count == 1. **Done** (tests/test_api_run.py), plus approval survives a server restart |
| 7 | Frontend | drive a whole run from the browser, with approval, swaps and pauses. **Done** (Next.js 16). LiveView dropped: Chrome opens on your own screen, so CAPTCHAs are solved there |
| 8 | Hardening + report | **Done:** USDA retries (earlier), per-run AI call cap and usage summary, realistic cooking-time floors, stricter product cuts, README demo script and troubleshooting, project report. **Not built:** Developer Platform link and computer-use fallback (optional) |

## 17. Tests

- **Unit:** validator gates (each one), hint math, USDA raw selection and energy fallback, cache hit/miss, solver hits targets, maps, consolidator, `parse_size`, guard.
- **Contract:** every `HitlKind` round-trips through `hitl/events.py`.
- **Integration:** graph run with the LLM and shopper mocked; checks state transitions, DB persistence, and a single browser launch across pauses.
- **E2E (manual):** the Phase 5–7 checklists. Screen-record one clean run for the demo.

## 18. Risks

| Risk | Mitigation |
|---|---|
| Accidental checkout | No code path; route guard; payment method removed |
| CAPTCHA / 2FA | Persistent real-Chrome profile (rare); chat + LiveView; you solve them |
| Instacart terms | Personal educational use, human speed, one account, never parallel; Developer Platform fallback |
| Layout changes | Role/text selectors; LLM picks from scraped data; computer-use fallback |
| Wrong macros | Raw-only convention, USDA raw preference, hand-checked aliases |
| Cart drift / duplicates | Snapshot, idempotent adds, coverage check against the GroceryList |
| LLM cost | Per-call logging, per-item small calls, repairs of failing days only, caching |
