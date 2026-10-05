from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.graph.graph import build_graph
from app.models import MealSpec
from app.planner import planner
from app.planner.llm import LLMQuotaError
from fakes import ScriptedLLM, fake_lookup, rough_day, week

MUSHROOM = [("button_mushrooms", 50)]


def spec(days=2) -> MealSpec:
    return MealSpec(zip_code="61801", days=days, include_snacks=False, dislikes=["mushrooms"],
                    macros={"calories": 2000, "protein_g": 150})


async def run(spec, llm):
    await db.init_db()
    run_id = await db.create_run(spec.user_id, spec.model_dump())
    events = []

    async def emit(e):
        events.append(e)

    state = await build_graph(InMemorySaver()).ainvoke(
        {"run_id": run_id, "spec": spec},
        config={"configurable": {"thread_id": run_id, "llm": llm, "lookup": fake_lookup, "emit": emit}},
    )
    return run_id, state, events


async def test_bad_plan_repaired_within_three_rounds():
    # Tuesday uses a disliked ingredient, which the solver can't fix. The first repair
    # doesn't fix it either; the second does. Monday is never re-sent.
    llm = ScriptedLLM({
        "plan": [week(rough_day("Monday"), rough_day("Tuesday", extra=MUSHROOM))],
        "repair": [week(rough_day("Tuesday", extra=MUSHROOM)), week(rough_day("Tuesday"))],
    })
    run_id, state, events = await run(spec(), llm)

    assert state["validation"].passed
    assert state["repair_attempts"] == 2
    assert [p for p, _ in llm.calls] == ["plan", "repair", "repair"]  # one call per round
    repair_prompts = [u for p, u in llm.calls if p == "repair"]
    assert all("Tuesday problems" in u and "Monday" not in u for u in repair_prompts)

    saved = await db.get_run(run_id)
    assert saved["status"] == "running"  # Phase 3 continues from here
    assert "button_mushrooms" not in str(saved["meal_plan_json"])
    messages = [e["message"] for e in events if e["type"] == "run.progress"]
    assert "Repair round 2/3" in messages and "Plan passed every check" in messages
    assert messages[-1].startswith("Grocery list ready:")
    assert state["__interrupt__"][0].value["kind"] == "plan_approval"  # paused before any shopping
    assert any(e["type"] == "plan.ready" for e in events)
    assert {i["name"] for i in saved["grocery_list_json"]["items"]} >= {"chicken_breast", "basmati_rice"}


async def test_gives_up_after_three_repairs():
    llm = ScriptedLLM({
        "plan": [week(rough_day("Monday", extra=MUSHROOM))],
        "repair": [week(rough_day("Monday", extra=MUSHROOM))] * 3,
    })
    run_id, state, events = await run(spec(days=1), llm)

    assert state["error"] == "plan_failed_validation"
    assert state["repair_attempts"] == 3
    saved = await db.get_run(run_id)
    assert saved["status"] == "failed" and saved["error"] == "plan_failed_validation"
    assert events[-1]["type"] == "run.failed" and any("mushrooms" in d for d in events[-1]["detail"])


async def test_passing_plan_needs_no_repair():
    llm = ScriptedLLM({"plan": [week(rough_day("Monday"))]})
    _, state, _ = await run(spec(days=1), llm)
    assert state["validation"].passed and state["repair_attempts"] == 0
    assert [p for p, _ in llm.calls] == ["plan"]


async def test_quota_error_fails_run_cleanly():
    llm = ScriptedLLM({"plan": [LLMQuotaError("gemini: free-tier quota reached; try again in 8m51s")]})
    run_id, state, events = await run(spec(days=1), llm)
    assert state["error"] == "llm_quota_exceeded"
    saved = await db.get_run(run_id)
    assert saved["status"] == "failed" and "try again in 8m51s" in saved["error"]
    assert events[-1]["type"] == "run.failed"


async def test_days_per_call_chunks_and_aligns_names():
    # Replies with wrong day labels are aligned by position.
    llm = ScriptedLLM({"plan": [week(rough_day("Day 1"), rough_day("Day 2")), week(rough_day("x"))]})
    plan = await planner.generate(spec(days=3), llm, days_per_call=2)
    assert [d.day for d in plan.days] == ["Monday", "Tuesday", "Wednesday"]
    assert len(llm.calls) == 2 and "Earlier days already use" in llm.calls[1][1]


def test_feasibility_warning():
    from app.intake.intake import feasibility_warnings
    s = MealSpec(zip_code="1", macros={"calories": 1500, "protein_g": 250})
    assert "67% of your calories" in feasibility_warnings(s)[0]
