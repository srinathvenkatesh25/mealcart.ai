# MealCart Agent

Plans a week of meals that hit your calorie and protein targets, checks every day against USDA
nutrition data, and fills your Instacart cart. It stops at a filled, verified cart.
**It never checks out. You do that yourself.**

- How it was built and why: [docs/project-report.md](docs/project-report.md)
- Full spec: [docs/build-plan.md](docs/build-plan.md)

## What you need

- A Mac with **Google Chrome** installed (the app drives your real Chrome)
- Python 3.11 and Node 20+
- An Instacart account **with no saved payment card** (remove it in Instacart's settings)
- Free API keys:
  - **Gemini** (the AI that plans meals): https://aistudio.google.com/apikey
  - **USDA FoodData Central** (nutrition data): https://api.data.gov/signup/
  - **Groq** (optional backup AI): https://console.groq.com

## Setup (once)

```bash
cd ~/mealcart-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
cd frontend
npm install
```

Open `.env` and fill in `LLM_API_KEY` (your Gemini key), `USDA_API_KEY`, and optionally `GROQ_API_KEY`.

Sign in to Instacart once, in the app's own Chrome profile (kept in `profiles/`, never committed):

```bash
cd ~/mealcart-agent
source .venv/bin/activate
python scripts/login.py
```

It asks for your email, then the code Instacart sends. Later, `python scripts/login.py --check`
tells you whether the saved session still works.

## Run

Two terminals. Put nothing after the commands: zsh treats `#` comments as arguments.

Terminal 1, the server (leave it running; don't add `--reload`, which would kill a run mid-shop):

```bash
cd ~/mealcart-agent
source .venv/bin/activate
uvicorn app.main:app --app-dir backend
```

Terminal 2, the page:

```bash
cd ~/mealcart-agent/frontend
npm run dev
```

Open http://localhost:3000. **After changing any backend code, restart terminal 1.**

## Demo script (about 5 minutes)

1. **Set targets.** The form is filled with defaults: 2,200 kcal, 140 g protein, Indian, no mushrooms
   or mayonnaise, 30 minutes per meal. Change anything, then click **Plan my week**.
2. **Watch the activity feed.** It shows the AI planning, software sizing every portion, and the
   checks. Lines labelled "AI model" appear only if a model's free quota runs out and it switches.
3. **Read the week.** Each day is a nutrition label: calories and protein against your targets.
   Every meal shows its own calories and protein, adding up to the day. Click a meal for its
   carbs and fat, each ingredient's grams, calories and protein, and the steps.
4. **Swap a meal you don't like.** Hover a meal, click **Swap**, say why, tick ingredients to avoid.
   One AI call replaces just that meal, the day is re-balanced, and the list is rebuilt.
5. **Check the grocery list.** Untick anything you already have. The card shows the estimated cost
   against your budget (once prices from an earlier cart are known).
6. **Click Fill my cart.** MealCart shops in a hidden Chrome window; the activity feed shows each item
   as it's added. A window appears only if you need to sign in. If your cart already has items it
   asks whether to empty it. To watch the browser instead, set `BROWSER_MODE=visible` in `.env`.
7. **Review the cart.** Every item is checked against the list (needed vs in cart, price, substitutes).
   Items the best store doesn't stock are bought at the next-best store (up to 3 stores), so the
   review groups items by store with each store's subtotal. Check out each store's cart yourself.

To drive the same flow without the page: `python scripts/run_cli.py`.

## When something goes wrong

| What you see | Why, and what to do |
|---|---|
| `zsh: number expected` or a folder named `#` | A `#` comment was pasted after a command. Run the commands exactly as written above. |
| The page loads but the form is missing at `http://192.168...:3000` | Only `http://localhost:3000` is supported; the server is private to this Mac on purpose. |
| "Can't reach the MealCart server" | Terminal 1 isn't running, or was stopped. Start it again. |
| A fix doesn't seem to apply | The server doesn't reload itself. Press Ctrl+C in terminal 1 and start it again. |
| "AI model: ... quota used up. Switching to ..." | Normal. Gemini's free tier gives each model about 20 requests a day. The run continues on the next model. |
| "Every AI model ... is out of free quota" | All models are used up for today. Wait (the message says how long) or add another key. |
| Slow run, "rate-limited; waiting Ns" | Running on Groq, whose free tier allows 8,000 tokens a minute. It's waiting, not stuck. |
| "Request too large" (413) | Groq's cap per request. The app splits the request and retries; if you still see it, you're on an old server: restart. |
| "Another run is already using the Instacart browser" | Another run, or `scripts/login.py`, has the profile open. Wait for it to finish or close its Chrome window, then retry. |
| "You're not signed in to Instacart" | Run `python scripts/login.py` again; Instacart ended the session. (In the default `auto` mode a window opens for the sign-in by itself.) |
| "Instacart asked for a human check" | Hidden Chrome can't pass a CAPTCHA. Set `BROWSER_MODE=visible` in `.env`, restart the server, run again and solve it in the window. |
| "Heads up: Main protein 'chicken' conflicts with your restriction..." | Your protein and diet contradict. The protein was ignored. Fix the form to avoid this. |
| "Guard blocked N checkout/payment requests" | Expected. The app's Chrome refuses checkout pages by design. |

## Tests

```bash
cd ~/mealcart-agent
source .venv/bin/activate
pytest -q                                  # all 218, about 75 seconds (includes real-Chrome tests)
pytest -q --ignore=tests/test_session.py   # without the Chrome tests, about 2 seconds
```

Tests use fake AI and a fake store; the running app never does.

## Scripts

| Script | What it does |
|---|---|
| `scripts/login.py` | Sign in to Instacart once (`--check`, `--manual`) |
| `scripts/run_cli.py` | A full run from the terminal, through the server |
| `scripts/plan_demo.py` | Plan a week with the live AI and USDA and print the checks |
| `scripts/shop_demo.py` | Fill the real cart from a fixed 5-item list |

## Project layout

```
backend/app/   api/ graph/ intake/ nutrition/ planner/ shopping/ hitl/ + config, db, models
frontend/      Next.js page (app/, components/, lib/)
tests/         unit, graph, API and real-Chrome tests
docs/          project report, build plan, original plan and review
```
