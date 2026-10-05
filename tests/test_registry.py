import asyncio

import pytest

from app import db
from app.graph.registry import HitlTimeoutError, RunRegistry
from app.hitl.events import EventBus


async def test_bus_numbers_replays_and_fans_out():
    bus = EventBus(maxlen=3)
    q = bus.subscribe()
    for i in range(5):
        await bus.publish({"type": "run.progress", "n": i})
    assert [e["seq"] for e in bus.replay(0)] == [3, 4, 5]  # bounded buffer
    assert [e["seq"] for e in bus.replay(4)] == [5]
    assert q.qsize() == 5
    bus.unsubscribe(q)
    await bus.publish({"type": "x"})
    assert q.qsize() == 5


async def test_ask_resolve_and_wrong_kind():
    await db.init_db()
    run_id = await db.create_run("user_default", {})
    reg = RunRegistry()
    task = asyncio.create_task(reg.ask(run_id, "email_code", "code?"))
    await asyncio.sleep(0.05)
    assert (await db.get_run(run_id))["status"] == "awaiting_hitl"
    assert not reg.resolve(run_id, "captcha", "done")          # wrong kind ignored
    assert not reg.resolve(run_id, "email_code", "1", event_id="hitl_other")  # stale event ignored
    assert reg.resolve(run_id, "email_code", "123456")
    assert await task == "123456"
    assert (await db.get_run(run_id))["status"] == "running"
    assert await db.get_open_hitl_event(run_id) is None
    assert not reg.resolve(run_id, "email_code", "again")       # nothing open now


async def test_unanswered_question_times_out():
    await db.init_db()
    run_id = await db.create_run("user_default", {})
    with pytest.raises(HitlTimeoutError):
        await RunRegistry(timeout_s=0.05).ask(run_id, "captcha", "solve it")
    assert await db.get_open_hitl_event(run_id) is None


async def test_restart_recovery_keeps_approvals_fails_the_rest():
    await db.init_db()
    approving = await db.create_run("user_default", {})
    shopping = await db.create_run("user_default", {})
    planning = await db.create_run("user_default", {})
    await db.log_hitl_event(approving, "plan_approval", {"prompt": "ok?"})
    await db.update_run(approving, status="awaiting_hitl")
    await db.log_hitl_event(shopping, "email_code", {"prompt": "code?"})
    await db.update_run(shopping, status="awaiting_hitl")

    assert await db.recover_runs_after_restart() == [approving]
    assert (await db.get_run(approving))["status"] == "awaiting_hitl"
    for run_id in (shopping, planning):
        run = await db.get_run(run_id)
        assert run["status"] == "failed" and run["error"] == "interrupted by server restart"
    assert await db.get_open_hitl_event(shopping) is None
