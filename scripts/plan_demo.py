"""Run planning end to end against the live LLM and USDA: python scripts/plan_demo.py [days]"""

import asyncio
import json
import sys
import time

sys.path.insert(0, "backend")

from app import db  # noqa: E402
from app.graph.graph import build_graph  # noqa: E402
from app.models import MealSpec  # noqa: E402
from app.nutrition.usda import USDAClient  # noqa: E402
from app.planner.llm import LLM  # noqa: E402


async def main(days: int) -> None:
    await db.init_db()
    spec = MealSpec(
        zip_code="61801", days=days, include_snacks=True,
        macros={"calories": 2200, "protein_g": 140},
        cuisines=["Indian"], protein_source="chicken",
        dislikes=["mushrooms", "mayonnaise"],
        max_prep_minutes=30, effort_level="low_moderate", cooking_skill="basic",
        equipment=["stove", "microwave", "oven", "rice_cooker"], budget_weekly_usd=60,
    )
    run_id = await db.create_run(spec.user_id, spec.model_dump())
    started = time.monotonic()

    async def emit(e):
        print(f"[{time.monotonic() - started:5.1f}s] {e.get('node', e['type'])}: {e.get('message', e.get('detail'))}")

    state = await build_graph().ainvoke(
        {"run_id": run_id, "spec": spec},
        config={"configurable": {"llm": LLM(), "lookup": USDAClient().get_macros_per_100g, "emit": emit},
                "recursion_limit": 50},
    )
    v = state["validation"]
    print("\nPASSED" if v.passed else "\nFAILED", f"after {state['repair_attempts']} repair round(s)")
    for day, t in v.per_day.items():
        print(f"  {day:9} {t['calories']:6.0f} kcal ({t['calories_delta']:+.0f})  {t['protein_g']:5.1f} g protein "
              f"({t['protein_g_delta']:+.1f})   carbs {t['carbs_g']:.0f} g, fat {t['fat_g']:.0f} g")
    for d in v.deltas:
        print("  -", d)
    for day in state["meal_plan"].days[:1]:
        print(f"\nSample day ({day.day}):")
        for m in day.meals:
            print(f"  {m.slot:11} {m.title} [{m.prep_minutes} min, {', '.join(m.equipment_used)}]")
            print("              " + ", ".join(f"{i.name} {i.grams:.0f}g" for i in m.ingredients))

    import aiosqlite
    async with aiosqlite.connect(db._db_path()) as conn:
        async with conn.execute("SELECT purpose, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) "
                                "FROM llm_calls WHERE run_id=? GROUP BY purpose", (run_id,)) as cur:
            print("\nLLM calls:", [tuple(r) for r in await cur.fetchall()])
    print(f"Total time {time.monotonic() - started:.0f}s | run {run_id}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
