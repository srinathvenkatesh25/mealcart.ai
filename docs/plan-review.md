# MealCart Agent plan: review and gaps

## Context
You asked me to read `mealcart-agent-plan.md`, say what I understood, say whether it covers everything, and find gaps that could stop it from doing what it is meant to do. Nothing gets built yet. This file holds the review and a short list of proposed changes to the plan.

---

## 1. What I understood

The app is a single-user, local tool:
1. **Input.** You fill in a form (v1) with your macros, number of days, cuisine, budget, ZIP code, allergies and protein source. This becomes a `MealSpec`.
2. **Planning.** An LLM writes a 7-day plan as structured output. Every ingredient has a snake_case name and a gram weight.
3. **Checking.** A deterministic validator looks up each ingredient's macros per 100g in USDA FoodData Central (cached in SQLite). It sums each day and checks: calories within ±5% and each macro within ±10g. If a day fails, it writes repair hints ("reduce X by ~90g") and the LLM repairs the plan. It tries at most 3 times.
4. **Grocery list.** A deterministic step sums the week's grams, removes allergens, rounds to amounts you can buy and adds substitutes from a YAML file.
5. **You approve the list** (a LangGraph `interrupt`).
6. **Shopping.** Playwright uses a saved browser profile so you stay logged in, then sets the ZIP and store. A Browser-Use LLM agent searches for each item and adds it to the cart. It returns the cart as JSON, and Playwright reads the cart back on its own to check that JSON.
7. **Pauses for you.** CAPTCHAs, email codes and substitution approvals go over a websocket to a Next.js UI and wait for your answer.
8. **End state is `cart_ready`.** You check out by hand.

The design is sound. Keeping arithmetic out of the LLM, using the LLM only where the website is unpredictable, and adding `user_id` everywhere from day one are all good choices.

## 2. Does it cover everything?

It covers the pipeline well. But several pieces would break when built as written, and some promises in the plan have no mechanism behind them. Below, items marked 🔴 would stop the plan from working or break its main guarantees. 🟠 means wrong results or a bad experience. 🟡 means inconsistencies or polish.

---

## 3. Gaps and loopholes

### 🔴 Critical

**C1. "The agent never pays" is enforced only by the prompt.**
"No checkout code path" is true of *your* code, not of the Browser-Use agent. That agent is an LLM clicking in a logged-in session that has a saved card and address. If it gets confused or reads something misleading on a page, it can click Checkout or Place order. Fixes, in layers:
- Use Playwright `page.route()` to block navigation to and requests for checkout and order-placement URLs.
- Add a Browser-Use step callback that stops the agent if the URL matches `/checkout|/store/checkout|/orders|/account`.
- Set `allowed_domains` to Instacart only.
- Remove saved payment methods from the Instacart account used for the demo. This is the strongest guarantee.
- Add a test that checks the route blocker actually rejects the checkout URL.

**C2. LangGraph `interrupt()` inside `shop` breaks rule 4 ("never restart the browser").**
When LangGraph resumes after an `interrupt()`, it **re-runs the whole node from the start**. An interrupt inside `shop` would therefore run `get_context()` again and start Browser-Use from scratch. It also needs a checkpointer (`AsyncSqliteSaver`) and a `thread_id`, and the plan mentions neither. Fix:
- Use `interrupt()` only at node boundaries (plan approval, between `consolidate` and `shop`).
- For pauses inside `shop` (email code, CAPTCHA, substitution), keep the browser and agent in a **process-level registry keyed by `run_id`**. The shop node awaits an `asyncio.Future`, and the websocket handler resolves it. The browser never leaves memory.
- Add the checkpointer and `thread_id = run_id`. Do not let the email code end up in checkpoint state, because checkpoints are written to disk, which goes against §13's rule that secrets are never logged.

**C3. The Browser-Use API in the plan is likely out of date.**
Recent `browser-use` releases (0.6 and later) dropped Playwright and drive Chrome directly over CDP. They also use their own LLM wrappers (their own chat-model classes) instead of LangChain models. `Browser(playwright_context=ctx)` will probably not exist. A pattern that still works: launch Chromium once with the persistent profile and `--remote-debugging-port`. Playwright attaches with `connect_over_cdp` for the deterministic phases A and C, and Browser-Use attaches with `cdp_url`. Pin the exact version in `requirements.txt` after checking. `>=0.2` will drift.

**C4. The substitution rule cannot be enforced.**
§10 says a swap is applied automatically only if the validator still passes. But the substitute is chosen *by the Browser-Use agent in the middle of its run*, and the agent cannot call Python. It will also pick substitutes that are not in the YAML file and have no macros. Fix: register a **custom Browser-Use action** (e.g. `propose_substitution(original, candidate_name)`). It calls the validator, which looks up the candidate in USDA, and either approves the swap or pauses for your approval. The task prompt must forbid swapping without calling this action. Use USDA macros for substitutes too, not hardcoded YAML numbers, so both use the same data.

**C5. The cart check compares against the wrong thing and ignores items already in the cart.**
- Phase C compares the scraped cart to the *agent's own JSON*. If the agent left out an item, both versions agree and the check passes. Compare against the **`GroceryList`**: every item present, with a quantity that covers the grams needed.
- If the cart already holds items from earlier sessions or a crashed run, they are counted. Take a snapshot at the start and either empty the cart (after asking you) or compare only what changed.
- A retry after a crash will **add items twice**. Make adding idempotent by reading the cart before each add.

**C6. Raw and cooked weights will break the validator.**
Rice is about 130 kcal/100g cooked and about 365 kcal/100g raw. Lentils and meat differ in the same way. The planner's `preparation` field is ignored, and the USDA "first result" can come back cooked, fried or breaded. Fixes:
- Require **raw/dry weights everywhere**. Say so in the prompt.
- Add "raw" to every USDA query.
- Prefer Foundation or SR Legacy matches whose description contains "raw".
- Handle Foundation foods that report energy under nutrient 2047/2048 instead of 1008.
- Keep a small hand-checked `ALIAS` map for the roughly 30 staples you will actually use.

### 🟠 Significant

**S1. The repair loop is the least reliable part. Use math to set portions.**
LLMs are bad at precise gram changes across 7 days × 5 meals, and resending the full plan costs a lot of tokens. A better split: the LLM picks meals and ingredients, then a **deterministic solver** (non-negative least squares or a small LP with `scipy`, with per-ingredient min/max bounds) sizes the grams for each day to hit the targets. The LLM repair is then only needed when the solver cannot reach the targets (ingredients that cannot hit the macros). This fits "the LLM never does arithmetic" and would make "passes within 3 rounds" close to certain. When the LLM does repair, send only the days that failed.

**S2. Targets you cannot reach burn all 3 repair rounds.**
"Targets taken as given" is fine, but 4/4/9 × your macros often does not equal your calorie target. Your own example: 140p/270c/75f works out to about 2315 kcal against a 2500 target. Hitting both within ±5% and ±10g is then very tight, or impossible for other inputs. Add a **warning only** at intake (no rejection) that shows the gap and offers to treat macros as the main target.

**S3. The budget is never enforced.**
`budget_weekly_usd` is collected and then ignored. The approval prompt shows "est. $47", but nothing calculates it. Add a price estimate (after the first run, from the last known cart prices) and a budget check after Phase C that warns before `cart_ready`.

**S4. The consolidator can't read allergies, and there is no edge back to repair.**
`build(plan)` has no `spec`, so it cannot see `allergies`. Its "return to repair loop" also has no edge in the graph. Move allergen checks into the **validator**, so they become repair hints automatically. Map allergens to ingredients (e.g. dairy → ghee, paneer, curd; tree nuts → cashew, almond), because matching on names misses most cases.

**S5. Pack sizes do not match what the store sells.**
"4 lb chicken" may only come as 1.5 lb packs, produce is sold "each", and so on. The agent must report pack size × count, and Phase C must check that the grams bought are at least the grams needed. Add a **pantry list** to `MealSpec` (spices, oil, rice you already have). Without it you buy spices every week and go over budget.

**S6. One 80-step agent run is too fragile for about 18 items.**
At roughly 4 to 6 steps per item, 18 items take more than 80 steps. A single long run also cannot send per-item progress events ("Added chicken thighs — $19.16") and is hard to retry part of. Run **one short agent task per item** in the same browser (about 10 steps each). This gives a natural progress event per item, easy retries, a smaller context and cheaper tokens.

**S7. Asking "Allow the agent to attempt the CAPTCHA?" is the wrong question.**
Solving CAPTCHAs by machine is exactly what gets accounts flagged, and it goes against the plan's own ToS concern. Change it to: "Solve the CAPTCHA in the open browser window, then click Done." The browser is headful on your own machine, so you can.

**S8. The ToS risk is real, and there is an official route.**
Instacart's terms prohibit automated access. The **Instacart Developer Platform API** builds a shoppable list page from your grocery list ("products link"). You open it and add everything to your cart with one click. There is no CAPTCHA, login automation or ban risk. Even if you keep Browser-Use as the main feature, build this as a **fallback for the demo**. It turns a live demo that fails because of a CAPTCHA into a working one.

**S9. Reconnecting and process lifetime.**
- Events sent while the websocket is disconnected are lost, and `GET /api/runs/{id}` does not return the open pause. Add an event sequence number with replay, or include `open_hitl_event` in the GET response.
- The run lives in a background `asyncio` task, so `uvicorn --reload` kills it mid-shop. Document running without `--reload`.
- Two runs cannot open the same persistent profile. Add a per-user lock.

**S10. The app doesn't do what the stated goal says.**
- The goal says "natural-language requirements" and a "chat", but v1 is a form and the `intake` node does nothing. Either make a natural-language-to-`MealSpec` step the intake (one structured-output call, easy to add) or reword the goal.
- **You never see the meal plan or recipes.** There is no UI for the plan, and the planner writes no cooking steps. Add a `PlanView` and a `steps` field on `Meal`.
- The "Edit" button on plan approval has no message format defined (`value` is only "approved").

### 🟡 Inconsistencies and polish
- Section references are off: the intro says phases are in §16 but they are in §17, and the layout comments refer to §8 for USDA when that is §9.
- HITL kinds don't match: the DB lists `captcha`, the websocket uses `captcha_decision`, and §11 emits `login_required`, which appears in neither. Define one `Literal` enum in `hitl/events.py`.
- `DATABASE_URL=sqlite+aiosqlite:///` is SQLAlchemy syntax, but `aiosqlite` takes a file path.
- Cooking oil and ghee are often left out by LLMs, so fat comes out too low. The prompt should require cooking fat as an explicit ingredient.
- Setting a ZIP on Instacart may change your saved delivery address. Note it, or restore it afterwards.
- `include_snacks=False` should exclude the `mid_morning` and `evening` slots, and the schema or validator should enforce that.
- Log LLM cost per run, both planner and agent tokens (listed in Phase 8 but has no home in the schema). Add a `runs.llm_cost_usd` column.
- An Instacart MCP connector is available in this environment. You've ruled out MCP and that's fine; I'm only mentioning it.

---

## 3b. Other ways to fill the cart besides browser-use

You don't need browser-use. Muse probably ran a hosted browser agent (or a built-in Instacart integration) behind the scenes. browser-use is the open-source stand-in for that, and it's only one option. Ranked:

| # | Approach | How it fills the cart | Pros | Cons |
|---|---|---|---|---|
| **A** | **Instacart Developer Platform API** (official) | `POST /idp/v1/products/products_link` with your GroceryList as line items (name, quantity, unit). The response is a URL to a shopping-list page; you pick the store and add everything with one click | Allowed by Instacart's terms. No login automation, CAPTCHAs or ban risk. Plain `httpx`, a few hours of work. Works the same in every demo | You click "add to cart" (the last ~10 seconds are manual). Needs a developer API key (dev key is self-serve; production needs approval). Prices appear only after you pick a store |
| **B** | **Playwright + "LLM picks the product"** (no agent framework) | Playwright runs a fixed sequence: search box → type item → scrape the top ~8 results as JSON (name, size, price). One LLM structured-output call returns `{index, qty, is_substitute}`. Playwright clicks Add | Deterministic, cheap (1 LLM call per item), easy to debug, and checkout can't be reached because no code path exists. Substitution checks plug in naturally (the validator runs between the pick and the click) | CSS selectors break when Instacart changes its layout (mitigate with role/text selectors such as `get_by_role("button", name="Add")`). Bot detection is still a risk |
| **C** | **A computer-use model API** driving your Playwright page | You write a ~100-line loop: screenshot → model returns click/type → Playwright runs it. Use it only as a fallback when B's selectors fail | The model handles UI it hasn't seen. You control every action (you can block checkout clicks in your loop) | Slower and costs more tokens per step than B. Still automation, so the same terms risk |
| D | Other agent frameworks: Stagehand (`act/extract/observe`), Skyvern, OpenAI's computer-use agent | Similar to browser-use | Stagehand's `extract()` with a schema works well for the cart read-back | Same terms and CAPTCHA issues; one more dependency |
| ✗ | Calling Instacart's internal GraphQL directly | — | — | Undocumented, against the terms, and breaks without warning. Don't |

**Decision (after your answer):** you want a fully automatic, Muse-style experience where you handle only login and CAPTCHA. So the plan uses **B as the main path**: Playwright runs the steps and an LLM picks each product. Login and CAPTCHA pause for you in the open browser window, the way Muse did it. If B can't find an element, it falls back to C for that item only. A is kept only as an emergency link for the demo. browser-use is dropped.

Changes to the original plan's §11–§12:
- `session.py`: persistent profile. **Login happens through the chat, like Muse.** You never touch the browser.
  1. Not logged in → chat asks for your Instacart email (`hitl.required kind=login_email`). Playwright types it in.
  2. Instacart emails or texts a one-time code → chat asks for it (`kind=email_code`). Playwright types it in. If your account uses a password, the chat asks for that instead (`kind=login_password`). The password is kept in memory only: never written to the DB, logs or checkpoints.
  3. A CAPTCHA appears → the backend takes a screenshot of the challenge and shows it in the chat (`kind=captcha`). A text or picture CAPTCHA: you type the answer in the chat. A press-and-hold or interactive one, which can't be passed through chat: the chat says "please complete it in the open browser window, then click Done". This is the only time you might touch the browser.
  4. After the first successful login the session is saved in `profiles/<user_id>/`, so later runs skip all of this until Instacart logs you out.
- Same browser throughout, never restarted.

### Handling CAPTCHA and login smoothly (the Muse + WhatsApp experience)
There are three layers. Most runs never get past the first.
1. **Prevent CAPTCHAs.** Use real Chrome (`channel="chrome"`), not bundled Chromium. Keep it headful with one persistent profile on the same machine and network. Act at human speed (small random delays). Never log in again if the session is still valid. CAPTCHAs mostly fire on a new device or a fresh login, so with a saved session they're rare.
2. **Ask you in the web UI (your choice: no phone or messaging bot).** Every pause (`login_email`, `email_code`, `login_password`, `captcha`, `substitution_approval`, `plan_approval`) shows up as a prompt card in the chat page, with a screenshot when relevant. It also fires a browser notification (Notification API) and a sound, so you notice it while you're in another tab. You reply in the card. Codes and passwords are never logged. The notification sits behind a small `notify()` interface so a Telegram or WhatsApp channel can be added later without changes elsewhere.
3. **Live-view panel for interactive CAPTCHAs.** The prompt card opens a **live view** inside the chat page: the backend streams screenshots of the page (~2 fps), and your taps on the image, including press-and-hold and drag, are sent to Playwright (`page.mouse.down/move/up` at the scaled coordinates). You solve a press-and-hold CAPTCHA from the chat page. This is how hosted agents like Muse do "take control". It's a new frontend component, `LiveView.tsx`, with its own websocket messages (`live.frame` and `live.input`).
- Never use paid CAPTCHA-solving services or let the LLM solve CAPTCHAs.

### Getting the cart right: what "accurate" means and how it's checked
Done = **every GroceryList item is in the cart with grams bought ≥ grams needed, or is listed in the report as a substitute you approved or as unavailable.** Nothing is added that isn't on the list.
- Product picking works from scraped data (name, size, unit, price), not screenshots. The LLM returns `{index, packs}`. Python works out `packs × pack_size ≥ grams_needed`. The LLM never does the arithmetic.
- Size parsing ("1.5 lb", "32 oz", "12 ct", "each ≈ 150g" table) converts every option to grams.
- Before each add, the cart is read again so nothing is added twice. Before the run, the cart is captured and, with your OK, emptied.
- After each add, the script checks that the cart count or the item's line went up. If not, it retries once, then flags the item.
- Phase C: scrape the whole cart → match every line to a GroceryList item → coverage table (needed vs bought, price) → any gap = the run ends `needs_attention` (not `cart_ready`) with the gaps listed.
- Budget check against `budget_weekly_usd` in the final report.
- Test: a fixed 5-item list run 3 times must produce the same cart each time; then the full weekly list.
- `shopper.py`: for each GroceryItem, `search(item)` → `scrape_results()` (role/text selectors) → `pick_product()` (one structured LLM call: `{index, packs, is_substitute}`) → if it's a substitute, run the validator and pause for your approval if needed → `click_add()` → send a progress event.
- A route blocker on checkout and order URLs stays as a safety net.
- Phase C checks the cart against the GroceryList.

(Earlier recommendation, kept for reference:) build **A as the main path** and **B as an optional "auto-fill" mode**. With A, most of the critical shopping issues in §3 go away: no checkout risk, no restart-on-pause problem, no CAPTCHA, no cart verification. The macro validator (your USP) and the substitution logic stay exactly as planned, because substitutions are decided before the link is created. B is for a demo where the cart fills itself. It replaces browser-use with about 150 lines of Playwright plus one LLM call per item, and it fixes C1, C3 and C4 by design.

## 4. Proposed changes to the plan (if you want me to update it)
1. Add a hard checkout guardrail section (C1) and add a "removed payment method" step to setup.
2. Rewrite the HITL mechanism: `interrupt()` only at node boundaries, a Future-based session registry for pauses during shopping, and a checkpointer (C2).
3. Replace the §12 snippet with the CDP-shared browser pattern and pin `browser-use` (C3).
4. Add a custom `propose_substitution` action that calls the validator (C4).
5. Check the cart against the GroceryList, snapshot the cart first, make adding idempotent (C5).
6. Raw-weight convention plus USDA query and energy-nutrient handling (C6).
7. Add a portion solver before the LLM repair (S1). Add the intake feasibility warning, budget gate, allergen map, pantry list and per-item agent tasks (S2–S6).
8. Add the Instacart Developer Platform fallback (S8). Add plan/recipe view and natural-language intake (S10).

## Verification (once built)
- Unit tests: raw vs cooked lookups, the solver hitting targets, allergen mapping, checks against the GroceryList.
- A guardrail test: the route blocker rejects navigation to checkout.
- An integration test: a fake email-code pause during `shop` resumes **without** a second browser launch (assert the launch count is 1).
- End-to-end: a 5-item list followed by a full list, with Phase C matching the GroceryList.
