from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.graph.graph import build_graph
from app.models import MealSpec
from app.planner import planner
from app.planner.llm import LLMQuotaError
from fakes import ScriptedLLM, fake_lookup, rough_day, week

MUSHROOM = [("button_mushrooms", 50)]


def spec(days=2) -> MealSpec:
    return MealSpec(zip_code="12345", days=days, include_snacks=False, dislikes=["mushrooms"],
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
    s = MealSpec(zip_code="12345", macros={"calories": 1500, "protein_g": 250})
    assert "67% of your calories" in feasibility_warnings(s)[0]


def vegetarian_spec(**kw) -> MealSpec:
    return MealSpec(zip_code="12345", days=1, include_snacks=False, protein_source="chicken",
                    dietary_restrictions=["vegetarian"], macros={"calories": 2000, "protein_g": 150}, **kw)


def test_main_protein_that_breaks_your_diet_is_dropped():
    from app.intake.intake import resolve_conflicts
    fixed, notes = resolve_conflicts(vegetarian_spec())
    assert fixed.protein_source is None
    assert notes == ["Main protein 'chicken' conflicts with your restriction 'vegetarian', so it was ignored. "
                     "Meals will use other proteins."]
    unchanged, none = resolve_conflicts(vegetarian_spec().model_copy(update={"protein_source": "paneer"}))
    assert unchanged.protein_source == "paneer" and none == []


def vegetarian_day():
    from fakes import meal
    from app.models import DayPlan
    return DayPlan(day="Monday", meals=[
        meal("breakfast", [("eggs", 150), ("onion", 50), ("vegetable_oil", 10)]),
        meal("lunch", [("masoor_dal", 120), ("basmati_rice", 150), ("vegetable_oil", 10)]),
        meal("dinner", [("masoor_dal", 120), ("basmati_rice", 150), ("onion", 50), ("vegetable_oil", 10)]),
    ])


async def test_conflict_is_reported_saved_and_kept_out_of_the_prompt():
    # The plan is vegetarian, so it's valid for the diet; whether it also hits the calorie
    # target isn't this test's business (repairs are scripted just in case).
    llm = ScriptedLLM({"plan": [week(vegetarian_day())], "repair": [week(vegetarian_day())] * 3})
    run_id, state, events = await run(vegetarian_spec(), llm)
    assert state["spec"].protein_source is None
    assert (await db.get_run(run_id))["meal_spec_json"]["protein_source"] is None
    assert any("conflicts with your restriction 'vegetarian'" in e["message"] for e in events)
    plan_prompt = llm.calls[0][1]  # user message; the system prompt is built from the resolved spec
    assert "chicken" not in plan_prompt.lower()


def test_prompt_spells_out_what_each_restriction_forbids():
    from app.planner import prompts
    system = prompts.planner_system(vegetarian_spec(dislikes=["mushrooms"]))
    assert "vegetarian (diet):" in system and "chicken" in system.split("vegetarian (diet):")[1].split("\n")[0]
    assert "mushrooms (dislike): mushrooms, shiitake" in system
    assert "Main protein source" not in prompts.planner_system(
        vegetarian_spec().model_copy(update={"protein_source": None}))


async def test_oversized_repair_is_split_until_it_fits():
    from app.planner.llm import LLMTooLargeError
    days = ["Monday", "Tuesday", "Wednesday", "Thursday"]
    plan = week(*[rough_day(d, extra=MUSHROOM) for d in days])
    hints = [f"{d} lunch: 'button_mushrooms' violates dislike 'mushrooms' — replace it" for d in days]
    llm = ScriptedLLM({"repair": [
        LLMTooLargeError("too big"),                      # all four days: rejected
        LLMTooLargeError("still too big"),                # Monday + Tuesday: rejected
        week(rough_day("Monday")),                        # Monday alone
        week(rough_day("Tuesday")),                       # Tuesday alone
        week(rough_day("Wednesday"), rough_day("Thursday")),  # Wednesday + Thursday fit
    ]})
    fixed = await planner.repair(spec(days=4), plan, hints, llm)
    sent = [u for _, u in llm.calls]
    assert [sum(f"{d} problems:" in u for d in days) for u in sent] == [4, 2, 1, 1, 2]
    assert all("button_mushrooms" not in str(d) for d in fixed.days)
    assert [d.day for d in fixed.days] == days


async def test_single_day_still_too_large_fails_clearly():
    from app.planner.llm import LLMTooLargeError
    llm = ScriptedLLM({"plan": [week(rough_day("Monday", extra=MUSHROOM))],
                       "repair": [LLMTooLargeError("too big")]})
    run_id, state, events = await run(spec(days=1), llm)
    assert state["error"] == "llm_too_large"
    assert (await db.get_run(run_id))["status"] == "failed" and events[-1]["error"] == "llm_too_large"


async def test_oversized_plan_request_is_split_and_later_days_see_earlier_ones():
    from app.planner.llm import LLMTooLargeError
    llm = ScriptedLLM({"plan": [
        LLMTooLargeError("too big"),                                # all four days
        week(rough_day("Monday"), rough_day("Tuesday")),            # first half fits
        week(rough_day("Wednesday"), rough_day("Thursday")),        # second half fits
    ]})
    plan = await planner.generate(spec(days=4), llm, days_per_call=4)
    assert [d.day for d in plan.days] == ["Monday", "Tuesday", "Wednesday", "Thursday"]
    assert "Earlier days already use" not in llm.calls[1][1]       # first half: nothing earlier
    assert "Earlier days already use" in llm.calls[2][1]           # second half: told what's used


def test_providers_without_keys_are_skipped():
    from app.config import Settings
    from app.planner.llm import build_targets
    s = Settings(llm_api_key="g", llm_model="gemini-3.8-flash", groq_api_key="", openrouter_api_key="k",
                 llm_fallbacks="gemini-3.7-flash,groq:openai/gpt-oss-120b,openrouter:x/y:free")
    assert [t.label for t in build_targets(s)] == ["gemini-3.8-flash", "gemini-3.7-flash", "openrouter:x/y:free"]
    s2 = s.model_copy(update={"groq_api_key": "k"})
    assert "groq:openai/gpt-oss-120b" in [t.label for t in build_targets(s2)]


async def test_graph_raises_unrealistic_meal_times_and_says_so():
    from fakes import meal
    quick = meal("lunch", [("chicken_breast", 200), ("basmati_rice", 100), ("vegetable_oil", 10)], prep_minutes=5)
    plan_day = rough_day("Monday")
    plan_day.meals[1] = quick
    llm = ScriptedLLM({"plan": [week(plan_day)], "repair": [week(plan_day)] * 3})
    run_id, state, events = await run(spec(days=1), llm)
    lunch = state["meal_plan"].days[0].meals[1]
    assert lunch.prep_minutes == 15 and "needs at least that long" in lunch.notes
    assert any(e.get("message") == "Raised 1 meal time(s) to realistic cooking times" for e in events)
    assert (await db.get_run(run_id))["meal_plan_json"]["days"][0]["meals"][1]["prep_minutes"] == 15
