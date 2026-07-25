import json
import re
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException, Query
from app.auth import get_current_user_id
from app.models import LogEntryCreate, WaterUpdate, PortionUpdate
from app.database import get_conn
from app.services.logging import (log_entry_for_user, source_label, FoodNotFound,
                                  update_entry_quantity)
from app.services.food_lookup import get_food_by_id, ensure_portions
from app.services.portion import build_options, guard_grams, portion_label
from app.services.portion_history import personal_prior

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
            """SELECT le.*, f.name AS food_name, f.brand AS food_brand, f.source AS food_source,
                      f.serving_g, f.serving_desc, f.portions_json
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.user_id=? AND DATE(le.eaten_at, ?) = ?
               ORDER BY le.eaten_at""",
            (uid, mod, target),
        ).fetchall()
        return [_format_entry(r, conn) for r in rows]


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
            """SELECT le.*, f.name AS food_name, f.brand AS food_brand, f.source AS food_source,
                      f.serving_g, f.serving_desc, f.portions_json
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.user_id=? AND DATE(le.eaten_at) BETWEEN ? AND ?
               ORDER BY le.eaten_at""",
            (uid, start, end),
        ).fetchall()
        return [_format_entry(r, conn) for r in rows]


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
    try:
        entry = log_entry_for_user(
            uid, body.food_id, body.quantity_g, body.source,
            notes=body.notes, eaten_at=body.eaten_at,
        )
    except FoodNotFound:
        raise HTTPException(404, "Food not found")
    return entry


def _owned_entry(uid: int, entry_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, food_id, quantity_g FROM log_entries WHERE id=?",
            (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Entry not found")
    if row["user_id"] != uid:
        raise HTTPException(403, "Forbidden")
    return row


@router.get("/{entry_id}/portions")
async def entry_portion_options(entry_id: int, request: Request):
    """Portion choices for one entry — "1 spear (30g)", "1 can or bottle (12 fl
    oz)", "your usual". Every gram figure comes from USDA, the food's own serving
    size, or this user's history, so picking one is grounded rather than guessed.

    USDA household weights are fetched here on first need (not while logging, so
    the capture stays fast) and cached on the food forever."""
    uid = get_current_user_id(request)
    row = _owned_entry(uid, entry_id)
    food = get_food_by_id(row["food_id"])
    if not food:
        raise HTTPException(404, "Food not found")
    food = await ensure_portions(food)
    return {"entry_id": entry_id, "quantity_g": row["quantity_g"],
            "options": build_options(food, row["quantity_g"], personal_prior(uid, row["food_id"]))}


@router.put("/{entry_id}/portion")
async def set_entry_portion(entry_id: int, request: Request, body: PortionUpdate):
    """Apply a portion the USER chose. Recorded as manual so it also becomes the
    anchor for future logs of this food (correct it once, we stop guessing)."""
    uid = get_current_user_id(request)
    row = _owned_entry(uid, entry_id)
    grams, note = guard_grams(get_food_by_id(row["food_id"]) or {}, body.quantity_g)
    try:
        entry = update_entry_quantity(uid, entry_id, round(grams, 1), manual=True)
    except FoodNotFound:
        raise HTTPException(404, "Entry not found")
    return {**entry, "portion_basis": body.basis or "manual",
            "portion_confidence": "high", "note": note}


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


def _format_entry(row, conn=None) -> dict:
    snap = json.loads(row["nutrients_snapshot_json"])
    entry = {
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
    # Household-serving info so the UI can show "≈ 5 cakes" next to grams.
    if "serving_g" in row.keys():
        entry["serving_g"] = row["serving_g"]
        entry["serving_desc"] = row["serving_desc"]
        # Foods with no serving size still have USDA measures — "2 large" beats
        # bare grams for a two-egg omelette.
        if "portions_json" in row.keys():
            entry["portion_label"] = portion_label(row["quantity_g"], row["serving_g"],
                                                   row["serving_desc"], row["portions_json"])
    for col in ("portion_basis", "portion_confidence"):
        if col in row.keys():
            entry[col] = row[col]
    if conn is not None and "food_source" in row.keys():
        entry["food_source"] = source_label(conn, row["food_id"], row["food_source"])
    return entry
