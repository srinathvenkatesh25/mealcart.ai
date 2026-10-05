"""LLM meal planning and targeted repair of failing days.

Days are planned in chunks of settings.llm_days_per_call (default: the whole
week in one call), because free tiers cap requests per day.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.config import get_settings
from app.models import DayPlan, MealPlan, MealSpec
from app.planner import prompts

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


async def generate(spec: MealSpec, llm: StructuredLLM, run_id: str | None = None,
                   days_per_call: int | None = None) -> MealPlan:
    per_call = days_per_call or get_settings().llm_days_per_call
    system = prompts.planner_system(spec)
    names = WEEKDAYS[: spec.days]
    days: list[DayPlan] = []
    for start in range(0, len(names), per_call):
        chunk = names[start : start + per_call]
        reply = await llm.structured(MealPlan, system, prompts.planner_user(chunk, days),
                                     purpose="plan", run_id=run_id, temperature=0.3)
        days += _align(reply, chunk)
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


async def repair(spec: MealSpec, plan: MealPlan, deltas: list[str], llm: StructuredLLM,
                 run_id: str | None = None) -> MealPlan:
    """One LLM call fixes every failing day; passing days are kept as they are."""
    grouped = hints_by_day(plan, deltas)
    if not grouped:
        return plan
    failing = [d for d in plan.days if d.day in grouped]
    reply = await llm.structured(MealPlan, prompts.planner_system(spec), prompts.repair_user(failing, grouped),
                                 purpose="repair", run_id=run_id, temperature=0.2)
    fixed = {d.day: d for d in _align(reply, [d.day for d in failing])}
    return MealPlan(days=[fixed.get(d.day, d) for d in plan.days])
