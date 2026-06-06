import json
from datetime import datetime, timezone
import pytest
from app.database import get_conn

REG = {"email": "rem@example.com", "password": "password123", "display_name": "R"}


def _uid(email):
    with get_conn() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


# ── Reminders CRUD ────────────────────────────────────────────────────────────

def test_reminders_require_auth(client):
    assert client.get("/api/reminders/").status_code == 401


def test_add_and_list_reminder(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/reminders/", json={"time_local": "08:30", "tz_offset": 300})
    assert r.status_code == 200
    lst = client.get("/api/reminders/").json()
    assert len(lst) == 1
    assert lst[0]["time_local"] == "08:30"
    assert lst[0]["enabled"] is True


def test_reject_bad_time(client):
    client.post("/api/auth/register", json=REG)
    assert client.post("/api/reminders/", json={"time_local": "25:99"}).status_code == 400


def test_toggle_and_delete_reminder(client):
    client.post("/api/auth/register", json=REG)
    rid = client.post("/api/reminders/", json={"time_local": "12:00"}).json()["id"]
    assert client.put(f"/api/reminders/{rid}", json={"enabled": False}).status_code == 200
    assert client.get("/api/reminders/").json()[0]["enabled"] is False
    assert client.delete(f"/api/reminders/{rid}").status_code == 200
    assert client.get("/api/reminders/").json() == []


def test_cannot_touch_others_reminder(client):
    client.post("/api/auth/register", json=REG)
    rid = client.post("/api/reminders/", json={"time_local": "09:00"}).json()["id"]
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json={**REG, "email": "other@example.com"})
    assert client.delete(f"/api/reminders/{rid}").status_code == 403


# ── Push subscriptions ────────────────────────────────────────────────────────

_SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


def test_vapid_key_endpoint(client):
    client.post("/api/auth/register", json=REG)
    assert "public_key" in client.get("/api/push/vapid-key").json()


def test_subscribe_stores_one_row_per_endpoint(client):
    client.post("/api/auth/register", json=REG)
    client.post("/api/push/subscribe", json=_SUB)
    client.post("/api/push/subscribe", json=_SUB)   # idempotent
    uid = _uid(REG["email"])
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id=?", (uid,)).fetchone()["c"]
    assert n == 1


def test_unsubscribe(client):
    client.post("/api/auth/register", json=REG)
    client.post("/api/push/subscribe", json=_SUB)
    client.post("/api/push/unsubscribe", json={"endpoint": _SUB["endpoint"]})
    uid = _uid(REG["email"])
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id=?", (uid,)).fetchone()["c"]
    assert n == 0


def test_push_test_needs_subscription(client, monkeypatch):
    from app.routers import push
    monkeypatch.setattr(push, "send_to_user", lambda uid, payload: 0)
    client.post("/api/auth/register", json=REG)
    assert client.post("/api/push/test", json={}).status_code == 400


# ── Scheduler ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduler_fires_due_reminder(client, monkeypatch):
    from app.services import scheduler

    client.post("/api/auth/register", json=REG)
    uid = _uid(REG["email"])
    # A reminder whose local time + tz_offset == the current UTC minute fires now.
    now = datetime.now(timezone.utc)
    cur_min = now.hour * 60 + now.minute
    hh, mm = divmod(cur_min, 60)   # tz_offset 0 → local == UTC
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO reminders (user_id, time_local, tz_offset, enabled) VALUES (?,?,?,1)",
            (uid, f"{hh:02d}:{mm:02d}", 0),
        )

    sent = []
    monkeypatch.setattr(scheduler, "send_to_user", lambda u, payload: sent.append(u) or 1)

    await scheduler._tick()
    assert uid in sent
    # last_fired set → won't fire again this minute
    sent.clear()
    await scheduler._tick()
    assert sent == []
