# MealCart Agent: project report

## 1. What it is

A single-user web app. You give it daily calorie and protein targets plus how you like to cook
(cuisine, time per meal, equipment, dislikes). It plans a week of meals, checks every day against
USDA nutrition data, then fills your Instacart cart using a real Chrome session. It stops at a
filled, verified cart. **It has no checkout code path, and the browser it opens refuses checkout
and payment pages.**

Size: about 3,400 lines of Python, 1,300 lines of TypeScript, 210 automated tests.

## 2. How a run works

```
form or sentence
  → intake        resolves contradictions (e.g. chicken + vegetarian), warns about odd targets
  → plan          AI picks meals and ingredients in raw grams
  → solve         software sizes every portion to the day's calories and protein
  → validate      deterministic checks; failures go back to the AI as specific hints (max 3 rounds)
  → consolidate   one grocery list for the week, pantry removed, substitutes attached
  → approve       you see the week and the list; approve, edit, swap a meal, or cancel
  → shop          Chrome on Instacart: pick a store, pick products, add to cart
  → verify        read the cart back from the page and compare it with the list
```

Nothing touches Instacart before you approve. Swapping a meal sends the run back through
solve → validate → consolidate and asks you again.

## 3. Design decisions that matter

| Decision | Why |
|---|---|
| **The AI never does arithmetic.** Portions come from a bounded least-squares solver over USDA numbers. | Language models are unreliable at gram-level maths across 35 meals. The solver makes "within 5% calories, 10 g protein" reachable on the first pass. |
| **Everything the AI is told, Python checks.** Allergies, dislikes, diet, prep time, equipment, step count, variety. | A prompt is a request, not a guarantee. Each failure becomes a named repair hint. |
| **Only calories and protein are targets.** Carbs and fat are shown, never gated. | Product decision: two targets are far easier to hit exactly, and these are the numbers the user cares about. |
| **Raw weights everywhere, "raw" in every USDA search.** | Rice is about 365 kcal per 100 g raw and 130 cooked; mixing them silently breaks the totals. |
| **No browser-agent framework.** Playwright runs fixed steps; the AI only chooses among scraped search results. | Deterministic, cheap (a few calls per run), debuggable, and checkout can't be reached because no code path exists. |
| **Pauses keep the browser open.** Login codes, CAPTCHAs, substitutions and "clear your cart?" wait in memory; the plan approval is checkpointed to disk. | Restarting the browser to ask a question would lose the session; secrets must never reach disk. |
| **Free AI only, with fallback across models and providers.** | The project is meant to cost nothing to run. |

## 4. Risks, and what we actually hit

| Risk | What happened | Mitigation |
|---|---|---|
| **Accidental purchase** | Not triggered, but the browser is logged in. | No checkout code path; a request guard aborts checkout, order and payment URLs and GraphQL operations (tested against a real browser); no saved card on the account; the guard fails closed. |
| **Guard crashing the login** | Instacart sends some POST bodies as binary; the guard read them as text and crashed mid-login. | Bodies decoded leniently; any request the guard can't check is blocked, not allowed. A binary request is now part of the browser tests. |
| **Site behaviour differs from assumptions** | The login code step has no Continue button; an item already in the cart shows only a collapsed "Quantity" button instead of Add. The second broke re-runs: they reported every item "unavailable". | Both handled and unit-tested from text captured live; a re-run of an already-filled cart adds nothing and verifies. |
| **Wrong nutrition match** | USDA's top hit for "turmeric, ground" was bison; "paneer" matched cream cheese; "sweet potato" matched the leaves. | Results must contain the food's own words; USDA's head name must match; raw preferred; aliases for staples; paneer taken from two USDA brand labels. 38 staples re-checked live. |
| **AI quota** | Gemini's free tier allows about 20 requests per model per day. A first design spent 6 on one plan. | Whole week per call, one call per repair round; four Gemini models in order, then Groq; a per-run cap of 40 calls; usage shown on the page. |
| **Small free tiers** | Groq caps a request at 8,000 tokens and 8,000 per minute. A repair of 7 days was 17,428 tokens and killed a run. | Oversized requests split in halves automatically (plan and repair); short rate limits are waited out, long ones switch model; every switch appears in the activity feed. |
| **Contradictory input** | "Vegetarian" ticked with "Main protein: chicken": every chicken meal failed validation and a full-week repair was attempted. | Conflicts resolved at intake with a visible notice; the prompt spells out what each restriction forbids. |
| **Optimistic AI timings** | "One-pot chicken pulao" labelled 5 minutes; "2-minute" snacks made from dried chickpeas. | Minimum cooking times for rice, dal, dried beans, raw meat and potato; meals can only be raised, and impossible ones are swapped. On real saved plans this corrected 10 to 14 of 35 meals and flagged 2 as impossible. |
| **Lost work on restart** | Stopping the server while waiting for approval resolved the question and failed the run on restart. | An unanswered approval stays open in the database; the run resumes from its checkpoint without re-planning. Other interrupted runs fail visibly. |
| **Stale server** | A fix didn't take effect because the server was never restarted. | The README says to restart after backend changes and lists the symptoms. |
| **Terms of service** | Instacart's terms discourage automated access. | Personal, human-paced use on one account, one run at a time (profile lock); the AI never solves CAPTCHAs, you do. |
| **Layout changes** | Selectors can break when Instacart changes its pages. | Role and accessible-name selectors, all in one file ([instacart.py](../backend/app/shopping/instacart.py)); parsers tested on captured page text. |

## 5. What was verified

- **210 automated tests.** Unit tests for the validator, solver, USDA ranking, pack-size parsing,
  consolidator, timing floors and the checkout guard; graph tests for repair, swap and approval;
  full API and websocket runs (login pause, approval after a server restart, reconnect replay,
  secrets absent from the database); and browser tests of login, saved sessions and the guard in
  real headless Chrome against a local fake site.
- **Live checks** against the real services: a 7-day plan from Gemini and from Groq passing every
  check; 38 USDA lookups; a real 5-item Instacart cart filled, re-run twice with nothing added,
  and read back as covered; the checkout guard blocking real checkout traffic; Groq's real 413.
- **Not covered by automation:** a full live plan-to-cart run is done by hand (it depends on live
  quotas and your Instacart session), and the web page was checked with scripted browser runs and
  screenshots rather than a UI test suite.

## 6. Known limits

- Mac and `http://localhost:3000` only. The server has no password and can drive your logged-in
  Instacart session, so it is deliberately not reachable from other devices.
- Depends on free AI tiers: a heavy day can use every model's quota, and Groq alone makes a full
  week slow (it waits for its per-minute allowance).
- Prices and totals are before fees and tax; the estimate before shopping needs prices from an
  earlier cart.
- Store choice is a heuristic: an AI shortlist, then a search probe for the hardest-to-find items.
- Pack sizes with unreadable text default to one pack with a note; loose produce uses an estimated
  weight per item (onion about 150 g), which affects how many are bought, never the nutrition.
- Carbs and fat are reported, not targeted.

## 6b. Not built, on purpose

- The official Instacart Developer API "shopping list link" as a no-browser fallback, and a
  computer-use fallback for single items. Both are optional and need extra keys or approval.
- Access from a phone or another device (needs a login step before the server is exposed).

## 7. Running it

See the [README](../README.md): setup, the two commands to run, a five-minute demo script, and a
troubleshooting table for every failure above.
