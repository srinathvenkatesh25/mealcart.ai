"""One async function per graph node.

Dependencies (llm, macro lookup, emit, ask, open_site) come from
config["configurable"] so tests can swap in fakes without touching the graph.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import ValidationError

from app import db
from app.graph.state import RunState
from app.hitl import events
from app.intake.intake import feasibility_warnings
from app.nutrition import solver, validator
from app.nutrition.usda import UnknownIngredientError
from app.planner import planner
from app.models import GroceryList
from app.planner.llm import LLMOutputError, LLMQuotaError
from app.shopping import consolidator, shopper

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
    meal_plan, missed = await solver.solve_plan(state["meal_plan"], state["spec"], lookup,
                                                only=state.get("solve_days"))
    await db.update_run(run_id, meal_plan_json=meal_plan.model_dump())
    msg = "Portions sized to hit calories and protein"
    await emit(events.progress(run_id, "solve", msg + (f" (still off: {', '.join(missed)})" if missed else "")))
    return {"meal_plan": meal_plan, "solve_days": None}


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
    await emit(events.plan_ready(run_id, state["meal_plan"].model_dump(), state["validation"].per_day,
                                 grocery_list.model_dump()))
    return {"grocery_list": grocery_list}


async def approve(state: RunState, config: RunnableConfig) -> RunState:
    """Pause until you approve (or edit) the grocery list. Nothing touches Instacart before this.

    A LangGraph interrupt: the run is checkpointed, so an approval still works
    after a server restart. This node re-runs from the top on resume, so it has
    no side effects before interrupt().
    """
    gl = state["grocery_list"]
    est = f" (est. ${gl.est_total_usd:.2f})" if gl.est_total_usd is not None else ""
    answer = interrupt({
        "kind": "plan_approval",
        "prompt": f"Grocery list ready: {len(gl.items)} items{est}. Approve to start filling your Instacart cart, "
                  "or swap a meal you don't like?",
    })
    if answer == "approved":
        return {"swap_request": None}
    if isinstance(answer, dict) and isinstance(answer.get("swap"), dict):
        return {"swap_request": answer["swap"]}
    if isinstance(answer, dict) and "edit" in answer:
        try:
            return {"grocery_list": GroceryList.model_validate(answer["edit"])}
        except ValidationError as e:
            await db.update_run(state["run_id"], status="failed", error=f"invalid edited list: {e}")
            return {"error": "invalid_edit"}
    await db.update_run(state["run_id"], status="failed", error="cancelled_by_user")
    return {"error": "cancelled_by_user"}


async def swap(state: RunState, config: RunnableConfig) -> RunState:
    """Replace one meal (one LLM call), then re-size, re-check and rebuild the list.

    Ingredients you name in `avoid` join the run's dislikes, so they are
    excluded from the new meal and from any later repair or swap.
    """
    llm, lookup, emit = _deps(config)
    req, run_id, plan = state["swap_request"], state["run_id"], state["meal_plan"]
    day, slot = str(req.get("day", "")), str(req.get("slot", ""))
    where = planner.find_meal(plan, day, slot)
    if where is None:
        await emit(events.progress(run_id, "swap", f"There's no {slot or '?'} on {day or '?'} to swap"))
        return {"swap_request": None, "swap_failed": True}

    spec = state["spec"]
    avoid = [a.strip() for a in req.get("avoid", []) if isinstance(a, str) and a.strip()]
    if avoid:
        spec = spec.model_copy(update={"dislikes": spec.dislikes + [a for a in avoid if a not in spec.dislikes]})
        await db.update_run(run_id, meal_spec_json=spec.model_dump())
    old = plan.days[where[0]].meals[where[1]]
    kcal = protein = 0.0
    for ing in old.ingredients:
        try:
            m = await lookup(ing.name)
        except UnknownIngredientError:
            continue
        kcal += ing.grams / 100 * m.calories
        protein += ing.grams / 100 * m.protein_g

    await emit(events.progress(run_id, "swap", f"Replacing {plan.days[where[0]].day} {old.slot}: {old.title}…"))
    try:
        new_plan = await planner.swap_meal(spec, plan, day, slot, str(req.get("reason", "")), kcal, protein,
                                           llm, run_id)
    except (LLMQuotaError, LLMOutputError) as e:
        return await _llm_failed(state, emit, e)
    new_meal = new_plan.days[where[0]].meals[where[1]]
    await emit(events.progress(run_id, "swap", f"New {old.slot}: {new_meal.title}"))
    return {"spec": spec, "meal_plan": new_plan, "swap_request": None, "swap_failed": False,
            "repair_attempts": 0, "solve_days": [new_plan.days[where[0]].day]}


def after_approve(state: RunState) -> str:
    if state.get("error"):
        return "stop"
    return "swap" if state.get("swap_request") else "shop"


def after_swap(state: RunState) -> str:
    if state.get("error"):
        return "stop"
    return "approve" if state.get("swap_failed") else "solve"


async def shop(state: RunState, config: RunnableConfig) -> RunState:
    llm, lookup, emit = _deps(config)
    c = config["configurable"]
    spec, run_id, gl = state["spec"], state["run_id"], state["grocery_list"]
    await db.update_run(run_id, grocery_list_json=gl.model_dump())  # in case it was edited
    await emit(events.progress(run_id, "shop", "Opening Instacart…"))
    async with c["open_site"](spec.user_id, c["ask"]) as site:
        report, complete = await shopper.fill_cart(spec, state["meal_plan"], gl, site=site, llm=llm,
                                                   lookup=lookup, ask=c["ask"], emit=emit, run_id=run_id)
    await db.update_run(run_id, cart_report_json=report.model_dump(),
                        status="cart_ready" if complete else "needs_attention")
    await emit(events.cart_result(run_id, complete, report.model_dump()))
    return {"cart_report": report}


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
