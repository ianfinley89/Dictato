import json
from datetime import datetime, timezone, timedelta
import pytest
from app.database import get_conn, purge_expired_cache

REG = {"email": "fs@example.com", "password": "password123", "display_name": "F"}


# ── food_description parser ────────────────────────────────────────────────────

def test_parse_per_100g():
    from app.services.fatsecret import _parse
    item = {"food_id": "1", "food_name": "Banana", "food_type": "Generic",
            "food_description": "Per 100g - Calories: 89kcal | Fat: 0.33g | Carbs: 22.84g | Protein: 1.09g"}
    f = _parse(item, "2099-01-01T00:00:00")
    assert f["serving_g"] is None          # genuinely per-100g
    assert f["serving_desc"] == "100 g"
    n = json.loads(f["nutrients_json"])
    assert n["calories"] == 89.0 and n["carbs_g"] == 22.84


def test_parse_per_serving():
    from app.services.fatsecret import _parse
    item = {"food_id": "2", "food_name": "Steak Burrito", "brand_name": "Chipotle",
            "food_description": "Per 1 burrito - Calories: 970kcal | Fat: 31.00g | Carbs: 118.00g | Protein: 48.00g"}
    f = _parse(item, "2099-01-01T00:00:00")
    assert f["serving_g"] == 100.0          # nominal 1 serving
    assert f["serving_desc"] == "1 burrito"
    assert f["brand"] == "Chipotle"
    assert json.loads(f["nutrients_json"])["calories"] == 970.0


def test_parse_unparseable_returns_none():
    from app.services.fatsecret import _parse
    assert _parse({"food_description": "no nutrition here"}, "2099-01-01") is None


# ── Brand fallback uses FatSecret before the AI pass ──────────────────────────

def test_brand_fallback_uses_fatsecret(client, monkeypatch):
    from app.services import food_lookup

    async def fake_usda(query, limit):  # USDA only has a wrong-brand burrito
        n = {"calories": 350, "protein_g": 12, "carbs_g": 40, "fat_g": 14, "fiber_g": 3, "micros": {}}
        return [{"source": "usda", "source_id": "TB1", "name": "Taco Bell Burrito Supreme",
                 "brand": None, "serving_desc": None, "serving_g": None, "nutrients_json": json.dumps(n)}]

    async def fake_fatsecret(query, limit):
        if "chipotle" not in query.lower():
            return []
        n = {"calories": 970, "protein_g": 48, "carbs_g": 118, "fat_g": 31, "fiber_g": 0, "micros": {}}
        return [{"source": "fatsecret", "source_id": "FS1", "name": "Steak Burrito",
                 "brand": "Chipotle", "serving_desc": "1 burrito", "serving_g": 100.0,
                 "nutrients_json": json.dumps(n), "expires_at": "2099-01-01T00:00:00"}]

    monkeypatch.setattr(food_lookup, "_search_usda", fake_usda)
    monkeypatch.setattr(food_lookup, "search_fatsecret", fake_fatsecret)
    client.post("/api/auth/register", json=REG)

    # Without a brand: USDA's Taco Bell wins (no FatSecret needed)
    plain = client.get("/api/foods/search?q=steak burrito").json()
    assert plain[0]["name"].startswith("Taco Bell")
    # With brand=chipotle: FatSecret match is surfaced first
    branded = client.get("/api/foods/search?q=steak burrito&brand=chipotle").json()
    assert branded[0]["brand"] == "Chipotle"
    assert branded[0]["nutrients_per_100g"]["calories"] == 970


# ── 24h purge ─────────────────────────────────────────────────────────────────

def _insert_fatsecret(name, expires_at) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json, expires_at) VALUES ('fatsecret', ?, ?, ?, ?)",
            (name, name, '{"calories":100,"protein_g":1,"carbs_g":1,"fat_g":1,"fiber_g":0,"micros":{}}', expires_at),
        )
        return cur.lastrowid


def test_purge_deletes_expired_unlogged(client):
    client.post("/api/auth/register", json=REG)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fid = _insert_fatsecret("Expired Snack", past)
    assert purge_expired_cache() >= 1
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM foods WHERE id=?", (fid,)).fetchone() is None


def test_purge_keeps_expired_but_logged(client):
    """A FatSecret food the user actually logged is kept (their diary record)."""
    client.post("/api/auth/register", json=REG)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fid = _insert_fatsecret("Logged Burrito", past)
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100})
    purge_expired_cache()
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM foods WHERE id=?", (fid,)).fetchone() is not None


def test_expired_fatsecret_hidden_from_search(client, monkeypatch):
    from app.services import food_lookup

    async def empty(query, limit):
        return []

    monkeypatch.setattr(food_lookup, "_search_usda", empty)
    monkeypatch.setattr(food_lookup, "_search_off", empty)
    monkeypatch.setattr(food_lookup, "search_fatsecret", empty)
    client.post("/api/auth/register", json=REG)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _insert_fatsecret("Ghostfruit", past)   # unique name, expired
    # It must not surface in search (expired licensed cache)
    assert client.get("/api/foods/search?q=ghostfruit").json() == []
