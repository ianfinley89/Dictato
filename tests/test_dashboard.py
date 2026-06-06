import json
from datetime import datetime, timezone, timedelta
import pytest
from app.database import get_conn

REG = {"email": "dash@example.com", "password": "password123", "display_name": "D"}


def _seed_food(cal=100.0) -> int:
    nutrients = {"calories": cal, "protein_g": 10.0, "carbs_g": 20.0, "fat_g": 5.0,
                 "fiber_g": 1.0, "micros": {}}
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json) VALUES (?,?,?,?)",
            ("manual", None, "Test Food", json.dumps(nutrients)),
        )
        return cur.lastrowid


def _log_on(user_email_uid, food_id, days_ago, calories):
    """Insert a log entry N days ago with a fixed calorie snapshot."""
    eaten = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    snap = {"calories": calories, "protein_g": 10.0, "carbs_g": 20.0, "fat_g": 5.0, "fiber_g": 1.0}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO log_entries
               (user_id, food_id, eaten_at, quantity_g, nutrients_snapshot_json, source, confirmed)
               VALUES (?,?,?,?,?,?,1)""",
            (user_email_uid, food_id, eaten, 100.0, json.dumps(snap), "manual"),
        )


def _uid(email):
    with get_conn() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


# ── Summary ───────────────────────────────────────────────────────────────────

def test_tz_modifier():
    from app.routers.log import _tz_modifier
    assert _tz_modifier(0) == "0 minutes"
    assert _tz_modifier(300) == "-300 minutes"   # US Central → shift UTC back 5h
    assert _tz_modifier(-60) == "60 minutes"      # UTC+1 → shift forward
    assert _tz_modifier(99999) == "-840 minutes"  # clamped


def test_summary_buckets_by_local_day(client):
    """A 02:00-UTC entry falls on a different calendar day for a US-Central user
    (offset 300 → local = UTC-5h → it's 21:00 the previous day)."""
    from datetime import datetime, timezone
    client.post("/api/auth/register", json=REG)
    uid = _uid(REG["email"])
    fid = _seed_food()
    eaten = datetime.now(timezone.utc).replace(hour=2, minute=0, second=0, microsecond=0).isoformat()
    with get_conn() as conn:
        snap = {"calories": 100, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "fiber_g": 0}
        conn.execute(
            """INSERT INTO log_entries (user_id, food_id, eaten_at, quantity_g, nutrients_snapshot_json, source, confirmed)
               VALUES (?,?,?,?,?,?,1)""",
            (uid, fid, eaten, 100.0, json.dumps(snap), "manual"),
        )
    s0 = client.get("/api/log/summary?days=3&tz_offset=0").json()
    s300 = client.get("/api/log/summary?days=3&tz_offset=300").json()
    day_utc = next(d["date"] for d in s0 if d["calories"] == 100)
    day_central = next(d["date"] for d in s300 if d["calories"] == 100)
    assert day_utc != day_central   # the same entry, bucketed to different local days


def test_summary_requires_auth(client):
    r = client.get("/api/log/summary")
    assert r.status_code == 401


def test_summary_returns_continuous_days(client):
    client.post("/api/auth/register", json=REG)
    r = client.get("/api/log/summary?days=7")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 7
    # All zeros, oldest first, dates strictly increasing
    assert all(d["calories"] == 0 for d in data)
    dates = [d["date"] for d in data]
    assert dates == sorted(dates)


def test_summary_aggregates_per_day(client):
    client.post("/api/auth/register", json=REG)
    uid = _uid(REG["email"])
    fid = _seed_food()
    _log_on(uid, fid, 0, 300)   # today
    _log_on(uid, fid, 0, 200)   # today again → 500 total
    _log_on(uid, fid, 2, 400)   # 2 days ago

    data = client.get("/api/log/summary?days=7").json()
    today = data[-1]
    two_ago = data[-3]
    assert today["calories"] == pytest.approx(500.0)
    assert two_ago["calories"] == pytest.approx(400.0)


def test_summary_scopes_by_user(client):
    client.post("/api/auth/register", json=REG)
    uid_a = _uid(REG["email"])
    fid = _seed_food()
    _log_on(uid_a, fid, 0, 999)

    # Second user should see none of user A's data
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json={**REG, "email": "dash2@example.com"})
    data = client.get("/api/log/summary?days=7").json()
    assert all(d["calories"] == 0 for d in data)


# ── Goals ─────────────────────────────────────────────────────────────────────

def test_update_goals(client):
    client.post("/api/auth/register", json=REG)
    r = client.put("/api/auth/goals", json={
        "calorie_goal": 2000, "protein_g": 150, "carbs_g": 200, "fat_g": 65,
    })
    assert r.status_code == 200
    assert r.json()["calorie_goal"] == 2000
    # Persisted
    me = client.get("/api/auth/me").json()
    assert me["protein_g"] == 150


def test_update_goals_requires_auth(client):
    r = client.put("/api/auth/goals", json={"calorie_goal": 2000})
    assert r.status_code == 401


def test_goals_allow_clearing(client):
    client.post("/api/auth/register", json=REG)
    client.put("/api/auth/goals", json={"calorie_goal": 2000})
    r = client.put("/api/auth/goals", json={})  # all None clears them
    assert r.status_code == 200
    assert r.json()["calorie_goal"] is None
