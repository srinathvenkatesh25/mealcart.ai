"""LLM meal planning and targeted repair of failing days.

Days are planned in chunks of settings.llm_days_per_call (default: the whole
week in one call), because free tiers cap requests per day.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.config import get_settings
from app.models import DayPlan, Meal, MealPlan, MealSpec
from app.planner import prompts
from app.planner.llm import LLMTooLargeError

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    async def structured(self, schema: type[T], system: str, user: str, *, purpose: str,
                         run_id: str | None = None, temperature: float = 0.3) -> T: ...


def _align(reply: MealPlan, names: list[str]) -> list[DayPlan]:
    """Match returned days to the requested names, by name first, then by position."""
    by_name = {d.day.strip().lower(): d for d in reply.days}
    out = []
    for i, name in enumerate(names):
        day = by_name.get(name.lower()) or (reply.days[i] if i < len(reply.days) else None)
        if day is None:
            raise ValueError(f"LLM returned {len(reply.days)} days, expected {names}")
        out.append(day.model_copy(update={"day": name}))
    return out


async def _plan_days(spec: MealSpec, system: str, chunk: list[str], earlier: list[DayPlan],
                     llm: StructuredLLM, run_id: str | None) -> list[DayPlan]:
    """Plan these days in one call; if the model won't take a request this size, split in two."""
    try:
        reply = await llm.structured(MealPlan, system, prompts.planner_user(chunk, earlier),
                                     purpose="plan", run_id=run_id, temperature=0.3)
    except LLMTooLargeError:
        if len(chunk) == 1:
            raise
        mid = len(chunk) // 2
        first = await _plan_days(spec, system, chunk[:mid], earlier, llm, run_id)
        return first + await _plan_days(spec, system, chunk[mid:], earlier + first, llm, run_id)
    return _align(reply, chunk)


async def generate(spec: MealSpec, llm: StructuredLLM, run_id: str | None = None,
                   days_per_call: int | None = None) -> MealPlan:
    per_call = days_per_call or get_settings().llm_days_per_call
    system = prompts.planner_system(spec)
    names = WEEKDAYS[: spec.days]
    days: list[DayPlan] = []
    for start in range(0, len(names), per_call):
        days += await _plan_days(spec, system, names[start : start + per_call], days, llm, run_id)
    return MealPlan(days=days)


def hints_by_day(plan: MealPlan, deltas: list[str]) -> dict[str, list[str]]:
    """Group validator hints by the day they start with; hints naming no day are dropped."""
    grouped: dict[str, list[str]] = {}
    for hint in deltas:
        for day in plan.days:
            if hint.startswith(day.day):
                grouped.setdefault(day.day, []).append(hint)
                break
    return grouped


async def _repair_days(spec: MealSpec, failing: list[DayPlan], grouped: dict[str, list[str]],
                       llm: StructuredLLM, run_id: str | None) -> list[DayPlan]:
    """Ask for a fix of these days; if the request is too big for the model, split it in two."""
    try:
        reply = await llm.structured(MealPlan, prompts.planner_system(spec), prompts.repair_user(failing, grouped),
                                     purpose="repair", run_id=run_id, temperature=0.2)
    except LLMTooLargeError:
        if len(failing) == 1:
            raise
        mid = len(failing) // 2
        return (await _repair_days(spec, failing[:mid], grouped, llm, run_id)
                + await _repair_days(spec, failing[mid:], grouped, llm, run_id))
    return _align(reply, [d.day for d in failing])


async def repair(spec: MealSpec, plan: MealPlan, deltas: list[str], llm: StructuredLLM,
                 run_id: str | None = None) -> MealPlan:
    """Fix the failing days (one call when it fits the model, several when it doesn't);
    passing days are kept as they are."""
    grouped = hints_by_day(plan, deltas)
    if not grouped:
        return plan
    failing = [d for d in plan.days if d.day in grouped]
    fixed = {d.day: d for d in await _repair_days(spec, failing, grouped, llm, run_id)}
    return MealPlan(days=[fixed.get(d.day, d) for d in plan.days])


def find_meal(plan: MealPlan, day: str, slot: str) -> tuple[int, int] | None:
    for di, d in enumerate(plan.days):
        if d.day.lower() == day.strip().lower():
            for mi, m in enumerate(d.meals):
                if m.slot == slot.strip().lower():
                    return di, mi
    return None


async def swap_meal(spec: MealSpec, plan: MealPlan, day: str, slot: str, reason: str, kcal: float,
                    protein: float, llm: StructuredLLM, run_id: str | None = None) -> MealPlan:
    """One LLM call replaces one meal; the rest of the week is untouched."""
    where = find_meal(plan, day, slot)
    if where is None:
        raise LookupError(f"no {slot} on {day}")
    di, mi = where
    old = plan.days[di].meals[mi]
    new = await llm.structured(Meal, prompts.planner_system(spec),
                               prompts.swap_user(plan, plan.days[di].day, old, reason, kcal, protein),
                               purpose="swap", run_id=run_id, temperature=0.5)
    updated = plan.model_copy(deep=True)
    updated.days[di].meals[mi] = new.model_copy(update={"slot": old.slot})
    return updated
