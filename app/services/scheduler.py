"""Background loop that fires meal reminders at each user's chosen local time.

Reminders store a local "HH:MM" plus the tz_offset (JS getTimezoneOffset) captured
when they were set, so we can compute the UTC minute to fire at:
    UTC = local + tz_offset   (since local = UTC - tz_offset)
A `last_fired` date guard ensures each reminder sends at most once per day.
"""
import asyncio
import time
from datetime import datetime, timezone
from app.database import get_conn, purge_expired_cache, purge_old_telemetry
from app.services.push import send_to_user

PROMPT = {
    "title": "Dictato",
    "body": "Have you eaten or drunk anything? Tap to log it.",
    "url": "/",
}


_last_purge = 0.0


async def reminder_loop():
    while True:
        try:
            await _tick()
            _maybe_purge()
        except Exception:
            pass  # never let the loop die
        await asyncio.sleep(60)


def _maybe_purge():
    """Hourly housekeeping: expired FatSecret cache (license requirement) and
    30-day-old telemetry (traces / server errors)."""
    global _last_purge
    if time.time() - _last_purge >= 3600:
        _last_purge = time.time()
        purge_expired_cache()
        purge_old_telemetry()


async def _tick():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    cur_minute = now.hour * 60 + now.minute

    with get_conn() as conn:
        rems = conn.execute(
            "SELECT id, user_id, time_local, tz_offset, last_fired FROM reminders WHERE enabled=1"
        ).fetchall()

    due = []
    for r in rems:
        try:
            hh, mm = map(int, r["time_local"].split(":"))
        except ValueError:
            continue
        fire_minute = (hh * 60 + mm + r["tz_offset"]) % (24 * 60)
        if fire_minute == cur_minute and r["last_fired"] != today:
            due.append(r)

    for r in due:
        await asyncio.to_thread(send_to_user, r["user_id"], PROMPT)
        with get_conn() as conn:
            conn.execute("UPDATE reminders SET last_fired=? WHERE id=?", (today, r["id"]))
