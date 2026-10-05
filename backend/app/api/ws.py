"""WS /api/runs/{run_id}/events?since=<seq> — live events in, answers out.

Client → server:  {"type": "hitl.response", "kind": "...", "value": ..., "event_id"?: "..."}
  plan_approval values: "approved" | "rejected" | {"edit": GroceryList}
                        | {"swap": {"day": "Tuesday", "slot": "dinner", "reason"?: "...", "avoid"?: ["paneer"]}}
Server → client:  the events in hitl/events.py, plus {"type": "hitl.rejected", "reason"} for
answers that don't match the open question (stale or wrong kind).
Answer values are never logged or stored.
"""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import db
from app.hitl import events

router = APIRouter()


@router.websocket("/api/runs/{run_id}/events")
async def run_events(ws: WebSocket, run_id: str, since: int = 0) -> None:
    state = ws.app.state
    if await db.get_run(run_id) is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    ctx = state.registry.get(run_id)
    queue = ctx.bus.subscribe()  # subscribe before replay so nothing falls in between
    last_sent = since

    async def send(event: dict) -> None:
        nonlocal last_sent
        if event.get("seq", 0) > last_sent:
            last_sent = event["seq"]
            await ws.send_json(event)

    async def sender() -> None:
        for event in ctx.bus.replay(since):
            await send(event)
        # After a server restart a paused approval exists only in the database: ask again.
        if not state.runner.is_active(run_id) and (open_event := await db.get_open_hitl_event(run_id)):
            await ws.send_json(events.hitl_required(run_id, open_event["id"], open_event["kind"],
                                                    open_event["payload"].get("prompt", "")))
        while True:
            await send(await queue.get())

    async def receiver() -> None:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "hitl.response":
                continue
            kind, value, event_id = msg.get("kind"), msg.get("value"), msg.get("event_id")
            if state.registry.resolve(run_id, kind, value, event_id):
                continue
            open_event = await db.get_open_hitl_event(run_id)
            if (open_event and open_event["kind"] == kind == "plan_approval"
                    and not state.runner.is_active(run_id)):
                await db.resolve_hitl_event(open_event["id"])
                state.runner.resume(run_id, value)
                continue
            await ws.send_json({"type": "hitl.rejected", "run_id": run_id, "kind": kind,
                                "reason": "no open question of that kind"})

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t
        ctx.bus.unsubscribe(queue)
