"""SQLite access. No ORM: small async helpers over aiosqlite.

Every domain table carries user_id (directly or via its run) so multi-user is
a data change, not a schema change.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL,              -- running | awaiting_hitl | cart_ready | needs_attention | failed
  meal_spec_json TEXT NOT NULL,
  meal_plan_json TEXT,
  grocery_list_json TEXT,
  cart_report_json TEXT,
  error TEXT,
  llm_cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usda_cache (
  query TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  cached_at TEXT NOT NULL
);

-- The user's response value is never stored here (codes/passwords are secrets).
CREATE TABLE IF NOT EXISTS hitl_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,              -- open | resolved
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
  user_id TEXT NOT NULL,
  item TEXT NOT NULL,
  product_name TEXT NOT NULL,
  pack_grams REAL NOT NULL,
  unit_price_usd REAL NOT NULL,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_item ON price_history(user_id, item);
"""

RUN_JSON_FIELDS = ("meal_spec_json", "meal_plan_json", "grocery_list_json", "cart_report_json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    return get_settings().database_path


async def init_db() -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.executescript(SCHEMA)
        await db.execute(
            "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
            (get_settings().single_user_id, _now()),
        )
        await db.commit()


async def create_run(user_id: str, meal_spec: dict[str, Any]) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = _now()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO runs (id, user_id, status, meal_spec_json, created_at, updated_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, user_id, json.dumps(meal_spec), now, now),
        )
        await db.commit()
    return run_id


async def get_run(run_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    run = dict(row)
    for field in RUN_JSON_FIELDS:
        if run[field] is not None:
            run[field] = json.loads(run[field])
    return run


_UPDATABLE = {"status", "error", "llm_cost_usd", *RUN_JSON_FIELDS}


async def update_run(run_id: str, **fields: Any) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update run fields: {sorted(unknown)}")
    values = {k: json.dumps(v) if k in RUN_JSON_FIELDS and v is not None else v for k, v in fields.items()}
    values["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in values)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*values.values(), run_id))
        await db.commit()


async def log_hitl_event(run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an open HITL event; returns {id, seq}. Seq is per-run, monotonically increasing."""
    event_id = f"hitl_{uuid.uuid4().hex[:12]}"
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM hitl_events WHERE run_id = ?", (run_id,)
        ) as cur:
            (seq,) = await cur.fetchone()
        await db.execute(
            "INSERT INTO hitl_events (id, run_id, seq, kind, payload_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (event_id, run_id, seq, kind, json.dumps(payload), _now()),
        )
        await db.commit()
    return {"id": event_id, "seq": seq}


async def resolve_hitl_event(event_id: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE hitl_events SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (_now(), event_id),
        )
        await db.commit()


async def get_open_hitl_event(run_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM hitl_events WHERE run_id = ? AND status = 'open' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    event = dict(row)
    event["payload"] = json.loads(event.pop("payload_json"))
    return event


async def get_usda_cache(query: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT payload_json FROM usda_cache WHERE query = ?", (query,)) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def put_usda_cache(query: str, payload: dict[str, Any]) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT OR REPLACE INTO usda_cache (query, payload_json, cached_at) VALUES (?, ?, ?)",
            (query, json.dumps(payload), _now()),
        )
        await db.commit()


async def record_price(user_id: str, item: str, product_name: str, pack_grams: float,
                       unit_price_usd: float) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO price_history (user_id, item, product_name, pack_grams, unit_price_usd, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, item, product_name, pack_grams, unit_price_usd, _now()),
        )
        await db.commit()


async def latest_prices(user_id: str) -> dict[str, tuple[float, float]]:
    """Most recent (pack_grams, unit_price_usd) per item, from earlier carts."""
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT item, pack_grams, unit_price_usd FROM price_history WHERE user_id = ? ORDER BY seen_at",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {item: (grams, price) for item, grams, price in rows}  # later rows win


async def log_llm_call(
    run_id: str | None, purpose: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO llm_calls (id, run_id, purpose, model, input_tokens, output_tokens, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, run_id, purpose, model, input_tokens, output_tokens, cost_usd, _now()),
        )
        if run_id is not None:
            await db.execute(
                "UPDATE runs SET llm_cost_usd = llm_cost_usd + ?, updated_at = ? WHERE id = ?",
                (cost_usd, _now(), run_id),
            )
        await db.commit()
