import re
from fastapi import APIRouter, Request, HTTPException
from app.auth import get_current_user_id
from app.models import ReminderCreate, ReminderUpdate
from app.database import get_conn

router = APIRouter(prefix="/api/reminders", tags=["reminders"])

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")   # HH:MM 24h


@router.get("/")
async def list_reminders(request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, time_local, enabled FROM reminders WHERE user_id=? ORDER BY time_local",
            (uid,),
        ).fetchall()
    return [{"id": r["id"], "time_local": r["time_local"], "enabled": bool(r["enabled"])} for r in rows]


@router.post("/")
async def add_reminder(req: ReminderCreate, request: Request):
    uid = get_current_user_id(request)
    if not _TIME_RE.match(req.time_local):
        raise HTTPException(400, "time_local must be HH:MM (24-hour)")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, time_local, tz_offset, enabled) VALUES (?,?,?,1)",
            (uid, req.time_local, max(-840, min(840, req.tz_offset))),
        )
        rid = cur.lastrowid
    return {"id": rid, "time_local": req.time_local, "enabled": True}


@router.put("/{reminder_id}")
async def update_reminder(reminder_id: int, req: ReminderUpdate, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Reminder not found")
        if row["user_id"] != uid:
            raise HTTPException(403, "Forbidden")
        conn.execute("UPDATE reminders SET enabled=? WHERE id=?", (1 if req.enabled else 0, reminder_id))
    return {"ok": True}


@router.delete("/{reminder_id}")
async def delete_reminder(reminder_id: int, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Reminder not found")
        if row["user_id"] != uid:
            raise HTTPException(403, "Forbidden")
        conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    return {"ok": True}
