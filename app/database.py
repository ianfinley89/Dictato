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

-- Every voice/photo/text capture, kept verbatim so we can analyze eating
-- patterns and coach the user later (what they said, what got logged, when).
CREATE TABLE IF NOT EXISTS capture_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    input_type TEXT NOT NULL,          -- 'voice' | 'photo' | 'text'
    transcript TEXT,                   -- voice-to-text or typed text (null for photo-only)
    summary TEXT,                      -- the agent's one-line "what I logged" sentence
    entries_json TEXT,                 -- snapshot of what got logged (names, qty, macros)
    fast_path INTEGER NOT NULL DEFAULT 0,
    meal TEXT,                         -- breakfast | lunch | dinner | snack | drink
    meal_label TEXT,                   -- short human name for the whole capture ("cereal with berries")
    tags_json TEXT,                    -- ["activity:cycling", "restaurant:taco-bell", ...]
    specificity TEXT,                  -- low | medium | high — how precise the user was
    photo_path TEXT,                   -- saved capture photo (dataset material; deleted with account)
    audio_path TEXT,                   -- saved voice note (dataset material + STT-failure replay; deleted with account)
    parent_capture_id INTEGER          -- set on follow-up refinements ("say more" / "add photo")
);

-- Durable per-user profile the coach builds up over time: goals context,
-- dietary facts, trends, preferences. facts_json is a free-form JSON blob.
CREATE TABLE IF NOT EXISTS user_profile (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    facts_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Coach chat history so conversations persist across sessions.
CREATE TABLE IF NOT EXISTS coach_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,                -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per model call (MLflow-style tracing, kept in-app): what was sent,
-- what came back, how long it took, what it cost — including failed calls.
CREATE TABLE IF NOT EXISTS model_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    feature TEXT NOT NULL,             -- 'agent' | 'coach'
    provider TEXT NOT NULL,            -- 'anthropic' | 'openai'
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    stop_reason TEXT,
    error TEXT,                        -- exception text when the call failed
    request_json TEXT,                 -- sanitized system+messages (images redacted)
    response_json TEXT                 -- text + tool calls
);

-- User-filed issue reports, with whatever context the app auto-attached.
-- Each report is auto-triaged into a pipeline: 'infra' bugs are code fixes,
-- 'model' reports are training-dataset candidates (the AI mis-handled food),
-- 'capture' means the mic/photo/transcription failed the user.
CREATE TABLE IF NOT EXISTS issue_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    message TEXT NOT NULL,
    context_json TEXT,
    category TEXT,                     -- 'infra' | 'model' | 'capture' | 'other' | NULL (admin can relabel)
    capture_id INTEGER                 -- links the report to the capture it's about
);

-- Unhandled server errors (the "normal software" telemetry).
CREATE TABLE IF NOT EXISTS app_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    method TEXT,
    path TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_user_eaten ON log_entries(user_id, eaten_at);
CREATE INDEX IF NOT EXISTS idx_foods_name ON foods(name);
CREATE INDEX IF NOT EXISTS idx_capture_user_created ON capture_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_coach_user_created ON coach_messages(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_traces_created ON model_traces(created_at);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    purge_expired_cache()


def purge_old_telemetry(days: int = 30) -> None:
    """Traces and server errors are debugging data, not user records — keep the
    DB small by dropping anything older than `days`. Issue reports are kept."""
    with get_conn() as conn:
        conn.execute("DELETE FROM model_traces WHERE created_at < datetime('now', ?)", (f"-{days} days",))
        conn.execute("DELETE FROM app_errors WHERE created_at < datetime('now', ?)", (f"-{days} days",))


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

    cap_cols = {r["name"] for r in conn.execute("PRAGMA table_info(capture_log)")}
    for col in ("meal", "meal_label", "tags_json", "specificity", "photo_path", "audio_path"):
        if col not in cap_cols:
            conn.execute(f"ALTER TABLE capture_log ADD COLUMN {col} TEXT")
    if "parent_capture_id" not in cap_cols:
        conn.execute("ALTER TABLE capture_log ADD COLUMN parent_capture_id INTEGER")

    iss_cols = {r["name"] for r in conn.execute("PRAGMA table_info(issue_reports)")}
    if "category" not in iss_cols:
        conn.execute("ALTER TABLE issue_reports ADD COLUMN category TEXT")
    if "capture_id" not in iss_cols:
        conn.execute("ALTER TABLE issue_reports ADD COLUMN capture_id INTEGER")


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
