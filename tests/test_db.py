import aiosqlite
import pytest

from app import db
from app.config import get_settings

EXPECTED_TABLES = {"users", "runs", "usda_cache", "hitl_events", "llm_calls", "price_history"}


async def test_init_db_creates_tables_and_seeds_user():
    await db.init_db()
    await db.init_db()  # idempotent
    async with aiosqlite.connect(get_settings().database_path) as conn:
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = {r[0] for r in await cur.fetchall()}
        async with conn.execute("SELECT id FROM users") as cur:
            users = [r[0] for r in await cur.fetchall()]
    assert EXPECTED_TABLES <= tables
    assert users == ["user_default"]


async def test_run_round_trip():
    await db.init_db()
    spec = {"zip_code": "61801", "macros": {"calories": 2500, "protein_g": 140}}
    run_id = await db.create_run("user_default", spec)

    run = await db.get_run(run_id)
    assert run["status"] == "running"
    assert run["meal_spec_json"] == spec
    assert run["meal_plan_json"] is None

    await db.update_run(run_id, status="cart_ready", grocery_list_json={"items": []})
    run = await db.get_run(run_id)
    assert run["status"] == "cart_ready"
    assert run["grocery_list_json"] == {"items": []}


async def test_get_missing_run_returns_none():
    await db.init_db()
    assert await db.get_run("run_nope") is None


async def test_hitl_events_seq_and_resolve():
    await db.init_db()
    run_id = await db.create_run("user_default", {})
    first = await db.log_hitl_event(run_id, "plan_approval", {"prompt": "ok?"})
    second = await db.log_hitl_event(run_id, "email_code", {"prompt": "code?"})
    assert (first["seq"], second["seq"]) == (1, 2)

    open_event = await db.get_open_hitl_event(run_id)
    assert open_event["id"] == second["id"]
    assert open_event["payload"] == {"prompt": "code?"}

    await db.resolve_hitl_event(second["id"])
    await db.resolve_hitl_event(first["id"])
    assert await db.get_open_hitl_event(run_id) is None


async def test_llm_call_cost_accumulates_on_run():
    await db.init_db()
    run_id = await db.create_run("user_default", {})
    await db.log_llm_call(run_id, "plan", "m", 1000, 500, 0.02)
    await db.log_llm_call(run_id, "repair", "m", 800, 400, 0.01)
    run = await db.get_run(run_id)
    assert run["llm_cost_usd"] == pytest.approx(0.03)


