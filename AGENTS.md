# AGENTS.md

Guidance for AI coding tools (and people) working in this repository.

MealCart plans a week of meals to a calorie and protein target, checks every day against USDA
data, and fills the user's Instacart cart(s) with Playwright. It is single-user and runs on the
user's own Mac. **It never checks out.**

## Layout

```
backend/app/
  api/         REST (runs.py) and the per-run websocket (ws.py)
  graph/       LangGraph flow (graph.py, nodes.py), background runner, pause registry
  intake/      request → MealSpec, conflict and feasibility checks
  nutrition/   USDA lookup + cache, validator, portion solver, cooking-time floors, maps
  planner/     LLM client with model fallback (llm.py), prompts, plan/repair/swap
  shopping/    grocery list, pack sizes, Chrome session, checkout guard, Instacart adapter, shopper
  hitl/        event shapes and the replayable event bus
frontend/      Next.js 16 page (app/, components/, lib/)
tests/         pytest; fakes.py holds the fake LLM, fake store and offline USDA table
scripts/       login, live demos, terminal client
docs/          build-plan.md (spec), project-report.md
```

## Commands

```bash
source .venv/bin/activate                     # Python 3.11
uvicorn app.main:app --app-dir backend        # API on :8000. Never add --reload: it kills a run mid-shop
cd frontend && npm run dev                    # page on :3000

pytest -q                                     # all tests, ~75 s, includes real headless Chrome
pytest -q --ignore=tests/test_session.py      # without the Chrome tests, ~2 s
cd frontend && npx tsc --noEmit && npx eslint .
```

The backend does not reload itself. After changing backend code, restart uvicorn, and only when no
run is in progress (`runs.status` is `running` or `awaiting_hitl`).

## Rules that must not be broken

1. **No checkout path.** Nothing may click checkout, place an order or touch payment.
   `shopping/guard.py` blocks checkout/order/payment URLs and GraphQL operations, and blocks any
   request it can't inspect. Never weaken it; extend its tests if you change it.
2. **Secrets stay in `.env`.** API keys never go in code, tests, docs, logs, commit messages or any
   `NEXT_PUBLIC_*` variable (those ship to the browser). `.env` is gitignored and kept read-only.
   Never overwrite it, e.g. with `cp .env.example .env`, when it already exists.
3. **Answers to pauses are never stored.** Login codes and passwords pass from the page straight to
   the waiting shopper (`graph/registry.py`), never into the database, checkpoints or logs.
4. **The LLM never does arithmetic.** Portions, macros, pack counts, prices and timings are computed
   in Python. Anything the LLM is told to respect is also checked in `nutrition/validator.py`.
5. **Tests never call live services.** Use `tests/fakes.py` (`ScriptedLLM`, `FakeSite`,
   `fake_lookup`). Live AI calls spend a small free quota (about 20 requests per Gemini model a day),
   and the live scripts (`plan_demo.py`, `shop_demo.py`, `run_cli.py`) use the real Instacart cart.
   Run them only when asked.
6. **All Instacart page knowledge lives in `shopping/instacart.py`:** selectors and text parsers,
   each parser tested on page text captured from the live site. When the site changes, update the
   parser and its captured-text test together.

## Conventions

- Ingredient names are canonical `snake_case`; grams are always **raw/dry** weights.
- Calories and protein are the only gated targets; carbs and fat are reported, not gated.
- Each Instacart store is a separate cart. A run may use up to `MAX_STORES` stores.
- Pause kinds are the `HitlKind` literal in `backend/app/models.py`; add new ones there first.
- The ZIP code is checked against Instacart's delivery address, never used to change it.
- Match the surrounding style: small modules, docstrings that say why, no new dependencies
  without a reason.
- Commit messages: a plain summary of what changed and why. No tool attribution or co-author
  trailers.

## Next.js 16

This Next.js version has breaking changes from older releases. Before writing frontend code, read
the relevant guide in `frontend/node_modules/next/dist/docs/` rather than relying on memory.
`agentRules: false` in `frontend/next.config.ts` stops the dev server from writing its own
instruction files into `frontend/`; keep it.
