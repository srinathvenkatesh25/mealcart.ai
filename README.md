# MealCart Agent

Plans a week of meals that hit your calorie and protein targets, checks them against
USDA data, and fills your Instacart cart. It stops at a filled, verified cart.
**It never checks out. You do that yourself.**

- Spec: [docs/build-plan.md](docs/build-plan.md)
- Why it changed from the first draft: [docs/plan-review.md](docs/plan-review.md)

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt
playwright install chrome
cp .env.example .env        # add LLM_API_KEY and USDA_API_KEY
```

Before any shopping run, remove the saved payment method from the Instacart account you use.

Sign in once in the app's own Chrome profile (saved in `profiles/`, never committed):

```bash
python scripts/login.py          # asks for email, then code or password, in the terminal
python scripts/login.py --check  # later: confirm the session is still saved
```

The app's Chrome blocks checkout and payment pages. Check out in your normal browser or app; the cart is saved to your account.

## Run

```bash
uvicorn app.main:app --app-dir backend     # no --reload: it would kill a run mid-shop
```

Until the web page exists (Phase 7), drive a full run from a second terminal:

```bash
python scripts/run_cli.py                  # default preferences inside the script
python scripts/run_cli.py my_spec.json     # or your own MealSpec JSON
```

It shows the week's plan and grocery list and asks you to approve. Type
`swap tuesday dinner` to replace a meal you don't like; the list is rebuilt and you're asked
again. Once you approve, it opens Chrome and fills
your Instacart cart, then prints the verified cart. Questions (approval, login code,
substitutions, clearing the cart) are asked in the terminal. It never checks out.

API: `POST /api/runs` (MealSpec JSON, or `{"text": "..."}`), `GET /api/runs/{id}`,
`WS /api/runs/{id}/events?since=<seq>`.

## Test

```bash
pytest
```

## Status

| Phase | State |
|---|---|
| 0 Scaffold | done |
| 1 Nutrition core | done |
| 2 Planner + solver + repair | done; try `python scripts/plan_demo.py 7` |
| 3 Grocery list | done |
| 4 Browser session + checkout guard | built; needs your one-time login: `python scripts/login.py` |
| 5 Shopper | done: live 5-item cart filled, re-runs add nothing, verified CART READY. Try: `python scripts/shop_demo.py` |
| 6 Server, websocket, approval and pauses | done; end-to-end: `python scripts/run_cli.py` |
| 7 to 8 | see docs/build-plan.md §16 |
