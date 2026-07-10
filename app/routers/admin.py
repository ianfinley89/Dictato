"""Maintainer-only usage & evaluation dashboard data.

Reads what production already records — capture_log (every voice/photo/text
capture), log_entries, ai_usage, coach_messages — and aggregates it into the
signals that tell you whether the app is working:

- activity: who's using it, how often, by which input method
- quality:  fast-path rate, zero-entry captures (the agent failed to log),
            and how entries are grounded (USDA vs web vs AI estimate)
- cost:     tokens/day and an approximate $ figure at Haiku rates

Access is limited to ADMIN_EMAILS (comma-separated env var).
"""
import json

from fastapi import APIRouter, Request, HTTPException, Query

from app.auth import get_current_user_id
from app.config import ADMIN_EMAILS
from app.database import get_conn

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Approximate $/1M tokens at Claude Haiku rates; a rough gauge, not a bill.
_IN_PER_M = 1.00
_OUT_PER_M = 5.00


def _require_admin(request: Request) -> int:
    uid = get_current_user_id(request)
    with get_conn() as conn:
        row = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row["email"].lower() not in ADMIN_EMAILS:
        raise HTTPException(403, "Admin only")
    return uid


@router.get("/stats")
async def stats(request: Request, days: int = Query(14, ge=1, le=90)):
    _require_admin(request)
    since = f"-{days} days"
    with get_conn() as conn:
        # Daily activity: captures by input type + distinct active users.
        daily = conn.execute(
            """SELECT DATE(created_at) AS day,
                      COUNT(*) AS captures,
                      SUM(CASE WHEN input_type='voice' THEN 1 ELSE 0 END) AS voice,
                      SUM(CASE WHEN input_type='photo' THEN 1 ELSE 0 END) AS photo,
                      SUM(CASE WHEN input_type='text'  THEN 1 ELSE 0 END) AS text,
                      COUNT(DISTINCT user_id) AS active_users
               FROM capture_log WHERE created_at >= datetime('now', ?)
               GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()

        totals = conn.execute(
            """SELECT COUNT(*) AS captures,
                      SUM(fast_path) AS fast_path,
                      SUM(CASE WHEN entries_json='[]' THEN 1 ELSE 0 END) AS zero_entry,
                      COUNT(DISTINCT user_id) AS active_users
               FROM capture_log WHERE created_at >= datetime('now', ?)""",
            (since,),
        ).fetchone()

        # Grounding quality: where logged nutrition came from.
        sources = conn.execute(
            """SELECT f.source, COUNT(*) AS n
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.created_at >= datetime('now', ?)
               GROUP BY f.source ORDER BY n DESC""",
            (since,),
        ).fetchall()

        # Cost: tokens per day across all users.
        tokens = conn.execute(
            """SELECT day, SUM(vision_calls) AS calls,
                      SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
               FROM ai_usage WHERE day >= DATE('now', ?)
               GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()

        coach_msgs = conn.execute(
            """SELECT COUNT(*) AS n FROM coach_messages
               WHERE role='user' AND created_at >= datetime('now', ?)""",
            (since,),
        ).fetchone()["n"]

        # Per-user snapshot (all time signup, windowed activity).
        per_user = conn.execute(
            """SELECT u.id, u.display_name, DATE(u.created_at) AS joined,
                      (SELECT MAX(created_at) FROM capture_log c WHERE c.user_id=u.id) AS last_capture,
                      (SELECT COUNT(*) FROM capture_log c WHERE c.user_id=u.id
                         AND c.created_at >= datetime('now', ?)) AS captures,
                      (SELECT COUNT(*) FROM log_entries le WHERE le.user_id=u.id
                         AND le.created_at >= datetime('now', ?)) AS entries,
                      (SELECT COALESCE(SUM(input_tokens)+SUM(output_tokens),0) FROM ai_usage a
                         WHERE a.user_id=u.id AND a.day >= DATE('now', ?)) AS tokens
               FROM users u ORDER BY captures DESC, u.id""",
            (since, since, since),
        ).fetchall()

    tin = sum(r["input_tokens"] or 0 for r in tokens)
    tout = sum(r["output_tokens"] or 0 for r in tokens)
    captures = totals["captures"] or 0
    return {
        "days": days,
        "totals": {
            "captures": captures,
            "active_users": totals["active_users"] or 0,
            "fast_path_pct": round(100 * (totals["fast_path"] or 0) / captures, 1) if captures else 0,
            "zero_entry_pct": round(100 * (totals["zero_entry"] or 0) / captures, 1) if captures else 0,
            "coach_messages": coach_msgs,
            "input_tokens": tin,
            "output_tokens": tout,
            "est_cost_usd": round(tin / 1e6 * _IN_PER_M + tout / 1e6 * _OUT_PER_M, 2),
        },
        "daily": [dict(r) for r in daily],
        "tokens_daily": [dict(r) for r in tokens],
        "entry_sources": [dict(r) for r in sources],
        "per_user": [dict(r) for r in per_user],
    }


@router.get("/traces")
async def traces(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Recent model calls, newest first — the MLflow-style trace feed."""
    _require_admin(request)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.id, t.created_at, t.feature, t.provider, t.model, t.latency_ms,
                      t.input_tokens, t.output_tokens, t.stop_reason, t.error,
                      t.request_json, t.response_json, u.display_name
               FROM model_traces t LEFT JOIN users u ON u.id = t.user_id
               ORDER BY t.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"traces": [dict(r) for r in rows]}


@router.get("/issues")
async def issues(request: Request, limit: int = Query(50, ge=1, le=200)):
    _require_admin(request)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.id, i.created_at, i.message, i.context_json, u.display_name
               FROM issue_reports i JOIN users u ON u.id = i.user_id
               ORDER BY i.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"issues": [dict(r) for r in rows]}


@router.get("/errors")
async def errors(request: Request, limit: int = Query(50, ge=1, le=200)):
    _require_admin(request)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, method, path, error FROM app_errors ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"errors": [dict(r) for r in rows]}


@router.get("/failures")
async def failures(request: Request, days: int = Query(14, ge=1, le=90), limit: int = Query(30, ge=1, le=200)):
    """Captures that logged nothing — the raw eval feed. Reading these transcripts
    is how you find parser gaps, STT misses, and prompt problems."""
    _require_admin(request)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.created_at, c.input_type, c.transcript, c.summary, u.display_name
               FROM capture_log c JOIN users u ON u.id = c.user_id
               WHERE c.entries_json='[]' AND c.created_at >= datetime('now', ?)
               ORDER BY c.id DESC LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall()
    return {"failures": [dict(r) for r in rows]}
