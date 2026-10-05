"""In-process state for live runs: the event bus and the one open question.

Pauses inside the shop step (login code, CAPTCHA, substitution, clearing the
cart) wait on an asyncio.Future here, so the browser stays open while you
answer. The answer goes straight to the waiting code: it is never written to
the database, a checkpoint or a log.
"""

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.hitl import events
from app.hitl.events import EventBus

HITL_TIMEOUT_S = 15 * 60


class HitlTimeoutError(Exception):
    pass


@dataclass
class Pending:
    event_id: str
    kind: str
    future: asyncio.Future


@dataclass
class RunContext:
    run_id: str
    bus: EventBus = field(default_factory=EventBus)
    pending: Pending | None = None


class RunRegistry:
    def __init__(self, timeout_s: float = HITL_TIMEOUT_S):
        self._runs: dict[str, RunContext] = {}
        self.timeout_s = timeout_s

    def get(self, run_id: str) -> RunContext:
        if run_id not in self._runs:
            self._runs[run_id] = RunContext(run_id)
        return self._runs[run_id]

    async def ask(self, run_id: str, kind: str, prompt: str, screenshot: bytes | None = None,
                  data: dict | None = None) -> Any:
        """Publish a question and wait for the answer. One open question per run."""
        ctx = self.get(run_id)
        record = await db.log_hitl_event(run_id, kind, {"prompt": prompt, **({"data": data} if data else {})})
        future = asyncio.get_running_loop().create_future()
        ctx.pending = Pending(record["id"], kind, future)
        await db.update_run(run_id, status="awaiting_hitl")
        shot = base64.b64encode(screenshot).decode() if screenshot else None
        await ctx.bus.publish(events.hitl_required(run_id, record["id"], kind, prompt, data, shot))
        try:
            answer = await asyncio.wait_for(future, self.timeout_s)
        except asyncio.CancelledError:
            # Server shutting down: leave the question open in the database. A paused
            # plan_approval is checkpointed and is asked again after the restart.
            ctx.pending = None
            raise
        except asyncio.TimeoutError:
            ctx.pending = None
            await db.resolve_hitl_event(record["id"])
            raise HitlTimeoutError(f"no answer to '{kind}' within {self.timeout_s / 60:.0f} min") from None
        ctx.pending = None
        await db.resolve_hitl_event(record["id"])
        await db.update_run(run_id, status="running")
        await ctx.bus.publish(events.hitl_resolved(run_id, record["id"], kind))
        return answer

    def resolve(self, run_id: str, kind: str, value: Any, event_id: str | None = None) -> bool:
        """Deliver an answer. Returns False for stale or mismatched answers, which are ignored."""
        pending = self._runs.get(run_id) and self._runs[run_id].pending
        if not pending or pending.kind != kind or (event_id and event_id != pending.event_id):
            return False
        if pending.future.done():
            return False
        pending.future.set_result(value)
        return True

    def asker(self, run_id: str):
        """An `ask(kind, prompt, screenshot)` bound to one run, as the shopper expects."""
        async def ask(kind: str, prompt: str, screenshot: bytes | None = None) -> Any:
            return await self.ask(run_id, kind, prompt, screenshot)
        return ask
