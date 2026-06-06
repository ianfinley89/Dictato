import json
import pytest
from app.database import get_conn

REG = {"email": "carol@example.com", "password": "password123", "display_name": "Carol"}


def _seed_food(calories_per_100g: float = 100.0) -> int:
    nutrients = {
        "calories": calories_per_100g, "protein_g": 10.0, "carbs_g": 10.0, "fat_g": 5.0,
        "fiber_g": 1.0, "micros": {},
    }
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json) VALUES (?,?,?,?)",
            ("manual", None, "Test Food", json.dumps(nutrients)),
        )
        return cur.lastrowid


def test_quantity_math(client):
    """200g of a 100-cal/100g food should yield 200 calories."""
    fid = _seed_food(calories_per_100g=100.0)
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/log/", json={"food_id": fid, "quantity_g": 200.0})
    assert r.status_code == 200
    assert r.json()["calories"] == pytest.approx(200.0, rel=1e-3)


def test_log_appears_in_today(client):
    fid = _seed_food()
    client.post("/api/auth/register", json=REG)
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100.0})
    r = client.get("/api/log/today")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_water_tracking(client):
    client.post("/api/auth/register", json=REG)
    assert client.get("/api/log/water").json()["glasses"] == 0
    r = client.post("/api/log/water", json={"glasses": 3})
    assert r.json() == {"glasses": 3, "goal": 8}
    assert client.get("/api/log/water").json()["glasses"] == 3
    # Setting is absolute (idempotent), and clamps at 0
    client.post("/api/log/water", json={"glasses": -5})
    assert client.get("/api/log/water").json()["glasses"] == 0


def test_water_requires_auth(client):
    assert client.get("/api/log/water").status_code == 401


def test_today_specific_date(client):
    """The day navigation passes a `date`; a day with no entries returns []."""
    fid = _seed_food()
    client.post("/api/auth/register", json=REG)
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100.0})
    # An arbitrary past date has nothing
    assert client.get("/api/log/today?date=2020-01-01").json() == []
    # A malformed date safely falls back to today (still shows the entry)
    assert len(client.get("/api/log/today?date=not-a-date").json()) == 1


def test_delete_entry(client):
    fid = _seed_food()
    client.post("/api/auth/register", json=REG)
    entry = client.post("/api/log/", json={"food_id": fid, "quantity_g": 50.0}).json()
    r = client.delete(f"/api/log/{entry['id']}")
    assert r.status_code == 200
    assert client.get("/api/log/today").json() == []


def test_cannot_delete_others_entry(client):
    """User A cannot delete user B's log entry."""
    fid = _seed_food()
    client.post("/api/auth/register", json=REG)
    entry = client.post("/api/log/", json={"food_id": fid, "quantity_g": 50.0}).json()
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json={**REG, "email": "other@example.com"})
    r = client.delete(f"/api/log/{entry['id']}")
    assert r.status_code == 403


def test_log_requires_auth(client):
    r = client.post("/api/log/", json={"food_id": 1, "quantity_g": 100})
    assert r.status_code == 401


def test_log_nonexistent_food_404(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/log/", json={"food_id": 99999, "quantity_g": 100.0})
    assert r.status_code == 404
