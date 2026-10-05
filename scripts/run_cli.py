"""Drive one full run through the server from the terminal (until the web page exists).

    # terminal 1
    uvicorn app.main:app --app-dir backend          # no --reload
    # terminal 2
    python scripts/run_cli.py                        # your saved preferences below
    python scripts/run_cli.py my_spec.json           # or a MealSpec JSON file

Plans the week, shows it, asks you to approve the grocery list, fills your
Instacart cart (Chrome opens), and prints the verified cart. Never checks out.
"""

import asyncio
import getpass
import json
import os
import sys

import httpx
import websockets

SERVER = os.environ.get("MEALCART_SERVER", "http://localhost:8000")
SECRET_KINDS = {"email_code", "login_password"}

DEFAULT_SPEC = {
    "days": 7,
    "macros": {"calories": 2200, "protein_g": 140},
    "cuisines": ["Indian"],
    "protein_source": "chicken",
    "dislikes": ["mushrooms", "mayonnaise"],
    "max_prep_minutes": 30,
    "effort_level": "low_moderate",
    "cooking_skill": "basic",
    "equipment": ["stove", "microwave", "oven", "rice_cooker"],
    "budget_weekly_usd": 60,
    "include_snacks": True,
}


def show_plan(msg: dict) -> None:
    print("\n=== Meal plan ===")
    for day in msg["meal_plan"]["days"]:
        t = msg["per_day"][day["day"]]
        print(f"\n{day['day']}: {t['calories']:.0f} kcal, {t['protein_g']:.0f} g protein")
        for meal in day["meals"]:
            print(f"  {meal['slot']:11} {meal['title']} ({meal['prep_minutes']} min)")
    print("\n=== Grocery list ===")
    for item in msg["grocery_list"]["items"]:
        print(f"  {item['name']:34} {item['purchase_qty']}")


def show_cart(msg: dict) -> None:
    r = msg["report"]
    print(f"\n=== {'CART READY' if msg['type'] == 'cart.ready' else 'NEEDS ATTENTION'} at {r['store']} — "
          f"${r['subtotal_usd']:.2f} ===")
    for c in r["coverage"]:
        print(f"  {'✓' if c['covered'] else '✗'} {c['item']:34} need {c['grams_needed']:.0f} g, "
              f"in cart {c['grams_in_cart']:.0f} g")
    for n in r["notes"]:
        print("  note:", n)
    print("\nCheck out yourself in the Instacart app or your normal browser.")


async def answer(msg: dict):
    kind, prompt = msg["kind"], msg["prompt"]
    if kind == "plan_approval":
        reply = (await asyncio.to_thread(
            input, f"\n{prompt}\n  yes | no | swap <day> <slot>   (e.g. swap tuesday dinner) > ")).strip()
        words = reply.lower().split()
        if words[:1] == ["swap"] and len(words) >= 3:
            reason = await asyncio.to_thread(input, "  Why? (optional) > ")
            avoid = await asyncio.to_thread(input, "  Ingredients to avoid from now on, comma-separated (optional) > ")
            return {"swap": {"day": words[1].capitalize(), "slot": words[2], "reason": reason.strip(),
                             "avoid": [a.strip() for a in avoid.split(",") if a.strip()]}}
        return "approved" if words[:1] and words[0].startswith("y") else "rejected"
    if kind == "captcha":
        await asyncio.to_thread(input, f"\n{prompt}\nPress Enter when done > ")
        return "done"
    reader = getpass.getpass if kind in SECRET_KINDS else input
    return await asyncio.to_thread(reader, f"\n{prompt}\n> ")


async def main(spec: dict) -> int:
    async with httpx.AsyncClient(base_url=SERVER, timeout=120) as http:
        resp = await http.post("/api/runs", json=spec)
        if resp.status_code != 200:
            print("Could not start:", resp.status_code, resp.text)
            return 1
        run = resp.json()
    print(f"Run {run['run_id']} started.")
    ws_url = SERVER.replace("http", "ws") + run["ws_url"]
    async with websockets.connect(ws_url, max_size=None) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg["type"]
            if kind == "run.progress":
                print(f"  [{msg['node']}] {msg['message']}")
            elif kind == "plan.ready":
                show_plan(msg)
            elif kind == "hitl.required":
                value = await answer(msg)
                await ws.send(json.dumps({"type": "hitl.response", "kind": msg["kind"], "value": value,
                                          "event_id": msg["event_id"]}))
            elif kind == "hitl.rejected":
                print("  (answer not accepted:", msg["reason"] + ")")
            elif kind in ("cart.ready", "run.needs_attention"):
                show_cart(msg)
                return 0 if kind == "cart.ready" else 2
            elif kind == "run.failed":
                print(f"\nRun failed: {msg['error']}", *msg["detail"], sep="\n  ")
                return 1
    return 1


if __name__ == "__main__":
    spec = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else dict(DEFAULT_SPEC)
    if not spec.get("zip_code"):
        spec["zip_code"] = os.environ.get("MEALCART_ZIP") or input("Your 5-digit ZIP code: ").strip()
    sys.exit(asyncio.run(main(spec)))
