"""Events streamed to the UI, and the per-run bus that numbers and replays them.

Server → client message types:
  run.progress     {node, message}
  plan.ready       {meal_plan, per_day, grocery_list}
  hitl.required    {event_id, kind, prompt, data?, screenshot_b64?}
  hitl.resolved    {event_id, kind}            (never the answer itself)
  cart.ready       {report}
  run.needs_attention {report}
  run.failed       {error, detail}
Every message carries run_id and a per-run `seq`; a client that reconnects
with ?since=<last seq> gets everything it missed.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]
Emit = Callable[[Event], Awaitable[None]]

REPLAY_BUFFER = 1000


def progress(run_id: str, node: str, message: str) -> Event:
    return {"type": "run.progress", "run_id": run_id, "node": node, "message": message}


def failed(run_id: str, error: str, detail: list[str]) -> Event:
    return {"type": "run.failed", "run_id": run_id, "error": error, "detail": detail}


def plan_ready(run_id: str, meal_plan: dict, per_day: dict, grocery_list: dict) -> Event:
    return {"type": "plan.ready", "run_id": run_id, "meal_plan": meal_plan, "per_day": per_day,
            "grocery_list": grocery_list}


def hitl_required(run_id: str, event_id: str, kind: str, prompt: str, data: dict | None = None,
                  screenshot_b64: str | None = None) -> Event:
    event = {"type": "hitl.required", "run_id": run_id, "event_id": event_id, "kind": kind, "prompt": prompt}
    if data:
        event["data"] = data
    if screenshot_b64:
        event["screenshot_b64"] = screenshot_b64
    return event


def hitl_resolved(run_id: str, event_id: str, kind: str) -> Event:
    return {"type": "hitl.resolved", "run_id": run_id, "event_id": event_id, "kind": kind}


def cart_result(run_id: str, complete: bool, report: dict) -> Event:
    return {"type": "cart.ready" if complete else "run.needs_attention", "run_id": run_id, "report": report}


async def no_emit(event: Event) -> None:
    return None


class EventBus:
    """Numbers a run's events, keeps the recent ones for replay, fans out to subscribers."""

    def __init__(self, maxlen: int = REPLAY_BUFFER):
        self.seq = 0
        self._buffer: deque[Event] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[Event]] = set()

    async def publish(self, event: Event) -> None:
        self.seq += 1
        numbered = {**event, "seq": self.seq}
        self._buffer.append(numbered)
        for queue in self._subscribers:
            queue.put_nowait(numbered)

    def replay(self, since: int) -> list[Event]:
        return [e for e in self._buffer if e["seq"] > since]

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)
