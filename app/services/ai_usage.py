import os
from datetime import datetime, timezone
from app.database import get_conn


def check_and_increment(user_id: int) -> bool:
    """Return True and bump vision_calls if under the daily limit, else False."""
    limit = int(os.getenv("AI_DAILY_LIMIT", "20"))
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT vision_calls FROM ai_usage WHERE user_id=? AND day=?",
            (user_id, today),
        ).fetchone()
        if (row["vision_calls"] if row else 0) >= limit:
            return False
        conn.execute(
            """INSERT INTO ai_usage (user_id, day, vision_calls)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, day) DO UPDATE SET vision_calls = vision_calls + 1""",
            (user_id, today),
        )
    return True


def record_tokens(user_id: int, input_tokens: int, output_tokens: int) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ai_usage (user_id, day, input_tokens, output_tokens)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET
                 input_tokens  = input_tokens  + excluded.input_tokens,
                 output_tokens = output_tokens + excluded.output_tokens""",
            (user_id, today, input_tokens, output_tokens),
        )


def get_today_usage(user_id: int) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT vision_calls, input_tokens, output_tokens FROM ai_usage WHERE user_id=? AND day=?",
            (user_id, today),
        ).fetchone()
    if not row:
        return {"vision_calls": 0, "input_tokens": 0, "output_tokens": 0}
    return dict(row)
