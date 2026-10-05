"""Event shapes sent to the UI. Phase 6 adds websocket delivery, replay and HITL."""

from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]
Emit = Callable[[Event], Awaitable[None]]


def progress(run_id: str, node: str, message: str) -> Event:
    return {"type": "run.progress", "run_id": run_id, "node": node, "message": message}


def failed(run_id: str, error: str, detail: list[str]) -> Event:
    return {"type": "run.failed", "run_id": run_id, "error": error, "detail": detail}


async def no_emit(event: Event) -> None:
    return None
