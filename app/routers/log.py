import json
import re
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException, Query
from app.auth import get_current_user_id
from app.models import LogEntryCreate, WaterUpdate
from app.database import get_conn
from app.services.food_lookup import get_food_by_id

router = APIRouter(prefix="/api/log", tags=["log"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WATER_GOAL = 8   # glasses/day


def _tz_modifier(tz_offset: int) -> str:
    """SQLite datetime modifier that converts a stored UTC timestamp to the
    client's local time. `tz_offset` is JS getTimezoneOffset() (minutes the local
    zone is *behind* UTC), e.g. 300 for US Central → '-300 minutes'."""
    tz_offset = max(-840, min(840, tz_offset))
    return f"{-tz_offset} minutes"


def _local_today(tz_offset: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=max(-840, min(840, tz_offset)))).date().isoformat()


@router.get("/today")
async def get_today(request: Request, tz_offset: int = 0, date: str | None = None):
    """Entries for one local day. `date` (YYYY-MM-DD, the user's local day) defaults
    to today; used by the main screen's day navigation."""
    uid = get_current_user_id(request)
    mod = _tz_modifier(tz_offset)
    target = date if (date and _DATE_RE.match(date)) else _local_today(tz_offset)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT le.*, f.name AS food_name, f.brand AS food_brand
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.user_id=? AND DATE(le.eaten_at, ?) = ?
               ORDER BY le.eaten_at""",
            (uid, mod, target),
        ).fetchall()
    return [_format_entry(r) for r in rows]


@router.get("/water")
async def get_water(request: Request, tz_offset: int = 0, date: str | None = None):
    uid = get_current_user_id(request)
    day = date if (date and _DATE_RE.match(date)) else _local_today(tz_offset)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT glasses FROM water_log WHERE user_id=? AND day=?", (uid, day)
        ).fetchone()
    return {"glasses": row["glasses"] if row else 0, "goal": WATER_GOAL}


@router.post("/water")
async def set_water(request: Request, body: WaterUpdate):
    uid = get_current_user_id(request)
    day = body.date if (body.date and _DATE_RE.match(body.date)) else _local_today(body.tz_offset)
    glasses = max(0, min(50, body.glasses))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO water_log (user_id, day, glasses) VALUES (?,?,?)
               ON CONFLICT(user_id, day) DO UPDATE SET glasses=excluded.glasses""",
            (uid, day, glasses),
        )
    return {"glasses": glasses, "goal": WATER_GOAL}


@router.get("/range")
async def get_range(request: Request, start: str, end: str):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT le.*, f.name AS food_name, f.brand AS food_brand
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.user_id=? AND DATE(le.eaten_at) BETWEEN ? AND ?
               ORDER BY le.eaten_at""",
            (uid, start, end),
        ).fetchall()
    return [_format_entry(r) for r in rows]


@router.get("/summary")
async def get_summary(request: Request, days: int = Query(7, ge=1, le=90), tz_offset: int = 0):
    """Per-day calorie/macro totals for the last `days` days in the client's local
    time zone, oldest first. Days with no entries are zeros so charts stay continuous."""
    uid = get_current_user_id(request)
    mod = _tz_modifier(tz_offset)
    today = (datetime.now(timezone.utc) - timedelta(minutes=max(-840, min(840, tz_offset)))).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DATE(eaten_at, ?) AS day,
                      SUM(json_extract(nutrients_snapshot_json, '$.calories'))  AS calories,
                      SUM(json_extract(nutrients_snapshot_json, '$.protein_g')) AS protein_g,
                      SUM(json_extract(nutrients_snapshot_json, '$.carbs_g'))   AS carbs_g,
                      SUM(json_extract(nutrients_snapshot_json, '$.fat_g'))     AS fat_g
               FROM log_entries
               WHERE user_id=? AND DATE(eaten_at, ?) >= ?
               GROUP BY day""",
            (mod, uid, mod, start),
        ).fetchall()
    by_day = {r["day"]: r for r in rows}

    out = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        r = by_day.get(d)
        out.append({
            "date": d,
            "calories": round(r["calories"] or 0, 1) if r else 0,
            "protein_g": round(r["protein_g"] or 0, 1) if r else 0,
            "carbs_g": round(r["carbs_g"] or 0, 1) if r else 0,
            "fat_g": round(r["fat_g"] or 0, 1) if r else 0,
        })
    return out


@router.post("/")
async def create_entry(request: Request, body: LogEntryCreate):
    uid = get_current_user_id(request)
    food = get_food_by_id(body.food_id)
    if not food:
        raise HTTPException(404, "Food not found")
    # A user's recipes/custom foods are private — can't log someone else's.
    if food["source"] in ("user", "recipe") and food.get("created_by_user_id") != uid:
        raise HTTPException(404, "Food not found")

    n = food["nutrients_per_100g"]
    factor = body.quantity_g / 100.0
    snapshot = {
        "calories": round((n.get("calories") or 0) * factor, 1),
        "protein_g": round((n.get("protein_g") or 0) * factor, 1),
        "carbs_g": round((n.get("carbs_g") or 0) * factor, 1),
        "fat_g": round((n.get("fat_g") or 0) * factor, 1),
        "fiber_g": round((n.get("fiber_g") or 0) * factor, 1),
    }
    eaten_at = body.eaten_at or datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO log_entries
               (user_id, food_id, eaten_at, quantity_g, nutrients_snapshot_json, source, notes, confirmed)
               VALUES (?,?,?,?,?,?,?,1)""",
            (uid, body.food_id, eaten_at, body.quantity_g, json.dumps(snapshot), body.source, body.notes),
        )
        entry_id = cur.lastrowid

    return {"id": entry_id, **snapshot}


@router.delete("/{entry_id}")
async def delete_entry(entry_id: int, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM log_entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Entry not found")
        if row["user_id"] != uid:
            raise HTTPException(403, "Forbidden")
        conn.execute("DELETE FROM log_entries WHERE id=?", (entry_id,))
    return {"ok": True}


def _format_entry(row) -> dict:
    snap = json.loads(row["nutrients_snapshot_json"])
    return {
        "id": row["id"],
        "food_id": row["food_id"],
        "food_name": row["food_name"],
        "food_brand": row["food_brand"],
        "eaten_at": row["eaten_at"],
        "quantity_g": row["quantity_g"],
        "source": row["source"],
        "notes": row["notes"],
        "calories": snap.get("calories", 0),
        "protein_g": snap.get("protein_g", 0),
        "carbs_g": snap.get("carbs_g", 0),
        "fat_g": snap.get("fat_g", 0),
        "fiber_g": snap.get("fiber_g", 0),
    }
