import sqlite3
import os
import re
from contextlib import contextmanager
from app.config import get_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    calorie_goal INTEGER,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS foods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT,
    name TEXT NOT NULL,
    brand TEXT,
    serving_desc TEXT,
    serving_g REAL,
    nutrients_json TEXT NOT NULL,
    expires_at TEXT,
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_foods_source_id
    ON foods(source, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    food_id INTEGER NOT NULL REFERENCES foods(id),
    eaten_at TEXT NOT NULL,
    quantity_g REAL NOT NULL,
    nutrients_snapshot_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    photo_path TEXT,
    confirmed INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS friends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    friend_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, friend_user_id)
);

CREATE TABLE IF NOT EXISTS shared_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    to_user_id INTEGER NOT NULL REFERENCES users(id),
    food_id INTEGER NOT NULL REFERENCES foods(id),
    quantity_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    origin_log_entry_id INTEGER REFERENCES log_entries(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_food_id INTEGER NOT NULL REFERENCES foods(id),
    ingredient_food_id INTEGER NOT NULL REFERENCES foods(id),
    quantity_g REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    food_id INTEGER NOT NULL REFERENCES foods(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, food_id)
);

CREATE TABLE IF NOT EXISTS water_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day TEXT NOT NULL,
    glasses INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients ON recipe_ingredients(recipe_food_id);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    endpoint TEXT NOT NULL,
    keys_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    time_local TEXT NOT NULL,
    tz_offset INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day TEXT NOT NULL,
    vision_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_log_user_eaten ON log_entries(user_id, eaten_at);
CREATE INDEX IF NOT EXISTS idx_foods_name ON foods(name);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    purge_expired_cache()


def purge_expired_cache() -> int:
    """Delete expired licensed-cache foods (FatSecret) that the user never logged.
    Logged foods are kept — they're the user's own diary record. Returns count deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM foods
               WHERE source='fatsecret' AND expires_at IS NOT NULL
                 AND datetime(expires_at) < datetime('now')
                 AND id NOT IN (SELECT food_id FROM log_entries)"""
        )
        return cur.rowcount


_SERVING_DESC_RE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]+)")
_BACKFILL_UNIT_G = {"g": 1, "grm": 1, "gram": 1, "grams": 1, "ml": 1, "mlt": 1,
                    "oz": 28.35, "onz": 28.35, "ounce": 28.35}


def _migrate(conn) -> None:
    """Idempotent schema upgrades for databases created before a column existed."""
    food_cols = {r["name"] for r in conn.execute("PRAGMA table_info(foods)")}
    if "serving_g" not in food_cols:
        conn.execute("ALTER TABLE foods ADD COLUMN serving_g REAL")
    if "expires_at" not in food_cols:
        conn.execute("ALTER TABLE foods ADD COLUMN expires_at TEXT")
    _backfill_serving_g(conn)

    rem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)")}
    if "tz_offset" not in rem_cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN tz_offset INTEGER NOT NULL DEFAULT 0")
    if "last_fired" not in rem_cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN last_fired TEXT")


def _backfill_serving_g(conn) -> None:
    """Recover serving_g for foods cached before the column existed, parsing a
    numeric serving_desc like '600.0 ml' or '30 g'. Idempotent (NULL rows only)."""
    rows = conn.execute(
        "SELECT id, serving_desc FROM foods WHERE serving_g IS NULL AND serving_desc IS NOT NULL"
    ).fetchall()
    for r in rows:
        m = _SERVING_DESC_RE.match(r["serving_desc"] or "")
        if not m:
            continue
        factor = _BACKFILL_UNIT_G.get(m.group(2).lower())
        if factor:
            conn.execute("UPDATE foods SET serving_g=? WHERE id=?",
                         (round(float(m.group(1)) * factor, 1), r["id"]))


@contextmanager
def get_conn():
    path = get_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
