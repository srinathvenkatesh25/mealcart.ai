"""One async function per graph node.

Dependencies (llm, macro lookup, emit) come from config["configurable"] so
tests can swap in fakes without touching the graph.
"""

from langchain_core.runnables import RunnableConfig

from app import db
from app.graph.state import RunState
from app.hitl import events
from app.intake.intake import feasibility_warnings
from app.nutrition import solver, validator
from app.planner import planner
from app.planner.llm import LLMOutputError, LLMQuotaError
from app.shopping import consolidator

MAX_REPAIR_ATTEMPTS = 3


def _deps(config: RunnableConfig):
    c = config["configurable"]
    return c["llm"], c["lookup"], c.get("emit", events.no_emit)


async def intake(state: RunState, config: RunnableConfig) -> RunState:
    _, _, emit = _deps(config)
    warnings = feasibility_warnings(state["spec"])
    for w in warnings:
        await emit(events.progress(state["run_id"], "intake", f"Heads up: {w}"))
    return {"warnings": warnings, "repair_attempts": 0}


async def plan(state: RunState, config: RunnableConfig) -> RunState:
    llm, _, emit = _deps(config)
    spec, run_id = state["spec"], state["run_id"]
    await emit(events.progress(run_id, "plan", f"Planning {spec.days} days…"))
    try:
        meal_plan = await planner.generate(spec, llm, run_id)
    except (LLMQuotaError, LLMOutputError, ValueError) as e:
        return await _llm_failed(state, emit, e)
    await db.update_run(run_id, meal_plan_json=meal_plan.model_dump())
    return {"meal_plan": meal_plan}


async def solve(state: RunState, config: RunnableConfig) -> RunState:
    _, lookup, emit = _deps(config)
    run_id = state["run_id"]
    meal_plan, missed = await solver.solve_plan(state["meal_plan"], state["spec"], lookup)
    await db.update_run(run_id, meal_plan_json=meal_plan.model_dump())
    msg = "Portions sized to hit calories and protein"
    await emit(events.progress(run_id, "solve", msg + (f" (still off: {', '.join(missed)})" if missed else "")))
    return {"meal_plan": meal_plan}


async def validate(state: RunState, config: RunnableConfig) -> RunState:
    _, lookup, emit = _deps(config)
    result = await validator.check(state["meal_plan"], state["spec"], lookup)
    msg = "Plan passed every check" if result.passed else f"{len(result.deltas)} problem(s) found"
    await emit(events.progress(state["run_id"], "validate", msg))
    return {"validation": result}


async def repair(state: RunState, config: RunnableConfig) -> RunState:
    llm, _, emit = _deps(config)
    attempt = state.get("repair_attempts", 0) + 1
    await emit(events.progress(state["run_id"], "repair", f"Repair round {attempt}/{MAX_REPAIR_ATTEMPTS}"))
    try:
        meal_plan = await planner.repair(state["spec"], state["meal_plan"], state["validation"].deltas,
                                         llm, state["run_id"])
    except (LLMQuotaError, LLMOutputError, ValueError) as e:
        return {**await _llm_failed(state, emit, e), "repair_attempts": attempt}
    return {"meal_plan": meal_plan, "repair_attempts": attempt}


async def _llm_failed(state: RunState, emit, e: Exception) -> RunState:
    error = "llm_quota_exceeded" if isinstance(e, LLMQuotaError) else "llm_bad_output"
    await db.update_run(state["run_id"], status="failed", error=f"{error}: {e}")
    await emit(events.failed(state["run_id"], error, [str(e)]))
    return {"error": error}


async def consolidate(state: RunState, config: RunnableConfig) -> RunState:
    _, _, emit = _deps(config)
    spec, run_id = state["spec"], state["run_id"]
    grocery_list = consolidator.build(state["meal_plan"], spec, await db.latest_prices(spec.user_id))
    await db.update_run(run_id, grocery_list_json=grocery_list.model_dump())
    est = f", est. ${grocery_list.est_total_usd:.2f}" if grocery_list.est_total_usd is not None else ""
    await emit(events.progress(run_id, "consolidate", f"Grocery list ready: {len(grocery_list.items)} items{est}"))
    return {"grocery_list": grocery_list}


async def fail(state: RunState, config: RunnableConfig) -> RunState:
    _, _, emit = _deps(config)
    detail = state["validation"].deltas
    await db.update_run(state["run_id"], status="failed", error="plan_failed_validation")
    await emit(events.failed(state["run_id"], "plan_failed_validation", detail))
    return {"error": "plan_failed_validation"}


def stop_on_error(state: RunState) -> str:
    return "stop" if state.get("error") else "continue"


def after_validate(state: RunState) -> str:
    if state["validation"].passed:
        return "done"
    if state.get("repair_attempts", 0) >= MAX_REPAIR_ATTEMPTS:
        return "fail"
    return "repair"
