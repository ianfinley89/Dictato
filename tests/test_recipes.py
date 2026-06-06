import json
import pytest
from app.database import get_conn

REG = {"email": "chef@example.com", "password": "password123", "display_name": "Chef"}
REG2 = {"email": "other@example.com", "password": "password123", "display_name": "Other"}


def _seed_public_food(name, cal, protein=0.0, carbs=0.0, fat=0.0) -> int:
    n = {"calories": cal, "protein_g": protein, "carbs_g": carbs, "fat_g": fat, "fiber_g": 0, "micros": {}}
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json) VALUES ('usda', NULL, ?, ?)",
            (name, json.dumps(n)),
        )
        return cur.lastrowid


# ── Recipes (ingredient-based) ────────────────────────────────────────────────

def test_recipe_computes_nutrition_per_serving(client):
    beef = _seed_public_food("Beef", 200.0, protein=26.0)
    potato = _seed_public_food("Potato", 80.0)
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/recipes/", json={
        "name": "Stew", "serving_label": "bowl", "servings": 4,
        "ingredients": [
            {"food_id": beef, "quantity_g": 400},      # 800 cal, 104 g protein
            {"food_id": potato, "quantity_g": 400},    # 320 cal
        ],
    })
    assert r.status_code == 200
    food = r.json()
    assert food["source"] == "recipe"
    assert food["serving_desc"] == "1 bowl"
    # total weight 800 g, 4 bowls → 200 g/bowl
    assert food["serving_g"] == pytest.approx(200.0)
    # total 1120 cal / 4 = 280 cal per bowl; per-100g = 1120/800*100 = 140
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(140.0, rel=1e-3)
    # Logging one bowl (200 g) yields 280 cal
    log = client.post("/api/log/", json={"food_id": food["id"], "quantity_g": food["serving_g"]})
    assert log.json()["calories"] == pytest.approx(280.0, rel=1e-2)


def test_custom_food_manual_macros(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/recipes/", json={
        "name": "My Coffee", "serving_label": "cup",
        "calories": 5, "protein_g": 0.3, "carbs_g": 1, "fat_g": 0,
    })
    assert r.status_code == 200
    food = r.json()
    assert food["source"] == "user"
    assert food["serving_g"] == pytest.approx(100.0)   # nominal: 1 serving
    log = client.post("/api/log/", json={"food_id": food["id"], "quantity_g": 100.0})
    assert log.json()["calories"] == pytest.approx(5.0)


def test_recipe_requires_name_or_macros(client):
    client.post("/api/auth/register", json=REG)
    assert client.post("/api/recipes/", json={"name": "Empty"}).status_code == 400


# ── Scoping / privacy ─────────────────────────────────────────────────────────

def test_user_food_is_private(client):
    client.post("/api/auth/register", json=REG)
    fid = client.post("/api/recipes/", json={"name": "Secret Sauce", "calories": 100}).json()["id"]
    # Owner can find it
    assert any(f["id"] == fid for f in client.get("/api/foods/search?q=secret").json())
    # Another user cannot search, get, or log it
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=REG2)
    assert all(f["id"] != fid for f in client.get("/api/foods/search?q=secret").json())
    assert client.get(f"/api/foods/{fid}").status_code == 404
    assert client.post("/api/log/", json={"food_id": fid, "quantity_g": 100}).status_code == 404


def test_user_food_wins_search(client):
    # Even a non-exact query should surface the user's own food without hitting USDA.
    client.post("/api/auth/register", json=REG)
    fid = client.post("/api/recipes/", json={"name": "Grandma's Special Stew", "calories": 250}).json()["id"]
    results = client.get("/api/foods/search?q=grandma").json()
    assert results[0]["id"] == fid


# ── Favorites & quick-picks ───────────────────────────────────────────────────

def test_favorite_and_quick_picks(client):
    fid = _seed_public_food("Banana", 89.0)
    client.post("/api/auth/register", json=REG)
    client.post(f"/api/foods/{fid}/favorite")
    quick = client.get("/api/foods/quick").json()
    assert any(f["id"] == fid for f in quick["favorites"])
    # Unfavorite
    client.delete(f"/api/foods/{fid}/favorite")
    assert client.get("/api/foods/quick").json()["favorites"] == []


def test_recents_in_quick_picks(client):
    fid = _seed_public_food("Oatmeal", 68.0)
    client.post("/api/auth/register", json=REG)
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100})
    quick = client.get("/api/foods/quick").json()
    assert any(f["id"] == fid for f in quick["recents"])


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_unused_recipe(client):
    client.post("/api/auth/register", json=REG)
    fid = client.post("/api/recipes/", json={"name": "Throwaway", "calories": 10}).json()["id"]
    assert client.delete(f"/api/recipes/{fid}").status_code == 200
    assert client.get("/api/foods/mine").json() == []


def test_cannot_delete_logged_recipe(client):
    client.post("/api/auth/register", json=REG)
    fid = client.post("/api/recipes/", json={"name": "Logged Meal", "calories": 300}).json()["id"]
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100})
    assert client.delete(f"/api/recipes/{fid}").status_code == 409
