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
    spec = {"zip_code": "12345", "macros": {"calories": 2500, "protein_g": 140}}
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




async def test_llm_usage_summary_per_run():
    await db.init_db()
    run_id = await db.create_run("user_default", {})
    other = await db.create_run("user_default", {})
    await db.log_llm_call(run_id, "plan", "gemini-3.7-flash", 1000, 8000, 0.0)
    await db.log_llm_call(run_id, "repair", "groq:openai/gpt-oss-120b", 2000, 500, 0.0)
    await db.log_llm_call(run_id, "repair", "groq:openai/gpt-oss-120b", 1500, 400, 0.0)
    await db.log_llm_call(other, "plan", "gemini-3.8-flash", 9, 9, 0.5)
    usage = await db.llm_usage(run_id)
    assert usage == {
        "calls": 3, "input_tokens": 4500, "output_tokens": 8900, "cost_usd": 0.0,
        "models": [{"model": "gemini-3.7-flash", "calls": 1, "tokens": 9000},
                   {"model": "groq:openai/gpt-oss-120b", "calls": 2, "tokens": 4400}],
    }
    assert (await db.llm_usage("run_none"))["calls"] == 0


async def test_database_created_before_nutrition_column_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE users (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
          meal_spec_json TEXT NOT NULL, meal_plan_json TEXT, grocery_list_json TEXT, cart_report_json TEXT,
          error TEXT, llm_cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO users VALUES ('user_default', 'x');
        INSERT INTO runs VALUES ('run_old', 'user_default', 'cart_ready', '{}', NULL, NULL, NULL, NULL, 0, 'x', 'x');""")
    con.commit(); con.close()
    monkeypatch.setenv("DATABASE_PATH", str(path))
    get_settings.cache_clear()
    await db.init_db()
    await db.init_db()                                   # and again: nothing to add the second time
    assert (await db.get_run("run_old"))["nutrition_json"] is None      # the old run still loads
    await db.update_run("run_old", nutrition_json={"per_day": {"Monday": {"calories": 1}}, "per_meal": {}})
    assert (await db.get_run("run_old"))["nutrition_json"]["per_day"]["Monday"]["calories"] == 1
