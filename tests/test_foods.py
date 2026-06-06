import json
import pytest
from app.database import get_conn

REG = {"email": "bob@example.com", "password": "password123", "display_name": "Bob"}


def _seed_food(name: str = "Test Rice Cake", calories: float = 390.0) -> int:
    nutrients = {
        "calories": calories, "protein_g": 8.0, "carbs_g": 80.0, "fat_g": 2.0,
        "fiber_g": 1.0, "micros": {},
    }
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json) VALUES (?,?,?,?)",
            ("manual", None, name, json.dumps(nutrients)),
        )
        return cur.lastrowid


def test_search_requires_auth(client):
    r = client.get("/api/foods/search?q=rice")
    assert r.status_code == 401


def test_search_returns_local_food(client):
    # A strong (lead-noun) match is served straight from cache without any network.
    _seed_food("Rice Cake")
    client.post("/api/auth/register", json=REG)
    r = client.get("/api/foods/search?q=rice cake")
    assert r.status_code == 200
    names = [f["name"] for f in r.json()]
    assert any("Rice Cake" in n for n in names)


def test_get_food_by_id(client):
    fid = _seed_food("Egg White")
    client.post("/api/auth/register", json=REG)
    r = client.get(f"/api/foods/{fid}")
    assert r.status_code == 200
    assert r.json()["name"] == "Egg White"


def test_get_nonexistent_food_404(client):
    client.post("/api/auth/register", json=REG)
    r = client.get("/api/foods/99999")
    assert r.status_code == 404


def test_backfill_serving_g_from_desc():
    """Foods cached before serving_g existed should get it backfilled from a
    numeric serving_desc."""
    from app.database import _backfill_serving_g, get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO foods (source, name, serving_desc, serving_g, nutrients_json) VALUES (?,?,?,?,?)",
            ("usda", "Old Soda", "600.0 ml", None, "{}"),
        )
        conn.execute(
            "INSERT INTO foods (source, name, serving_desc, serving_g, nutrients_json) VALUES (?,?,?,?,?)",
            ("usda", "Household Only", "1 can", None, "{}"),  # not parseable → stays None
        )
        _backfill_serving_g(conn)
        rows = {r["name"]: r["serving_g"] for r in conn.execute("SELECT name, serving_g FROM foods")}
    assert rows["Old Soda"] == 600.0
    assert rows["Household Only"] is None


def test_cached_composite_does_not_mask_ingredient(client, monkeypatch):
    """A stray cached 'Tomato and cucumber salad' must not be served for 'tomato' —
    the strong-match gate should fall through to a fresh lookup."""
    import json as _json
    from app.services import food_lookup

    _seed_food("Tomato And Cucumber Salad Made With Tomato And Cucumber")  # weak cache pollution

    async def fake_usda(query, limit):
        n = {"calories": 18.0, "protein_g": 0.9, "carbs_g": 3.9, "fat_g": 0.2, "fiber_g": 1.2, "micros": {}}
        return [{"source": "usda", "source_id": "TOM1", "name": "Tomatoes, Raw",
                 "brand": None, "serving_desc": None, "serving_g": None,
                 "nutrients_json": _json.dumps(n)}]

    monkeypatch.setattr(food_lookup, "_search_usda", fake_usda)
    client.post("/api/auth/register", json=REG)
    top = client.get("/api/foods/search?q=tomato").json()[0]
    assert top["name"] == "Tomatoes, Raw"


def test_merge_usda_promotes_generic_ingredient():
    """A generic 'Tomatoes, raw' (from the generic call) should outrank a branded
    product named 'TOMATO' even though relevance put the brand first."""
    from app.services.food_lookup import _merge_usda
    generic = [{"fdcId": 1, "dataType": "Survey (FNDDS)", "description": "Tomatoes, raw"}]
    relevance = [
        {"fdcId": 2, "dataType": "Branded", "description": "TOMATO"},
        {"fdcId": 3, "dataType": "Branded", "description": "Abc's Pasta In Tomato Sauce"},
    ]
    merged = _merge_usda(generic, relevance, "tomato", 10)
    assert merged[0]["description"] == "Tomatoes, raw"
    assert {f["fdcId"] for f in merged} == {1, 2, 3}   # deduped, all present


def test_merge_usda_ignores_composite_dish():
    """'cucumber' must promote 'Cucumber, raw', not 'Cucumber salad made with…'."""
    from app.services.food_lookup import _merge_usda
    generic = [
        {"fdcId": 20, "dataType": "Survey (FNDDS)", "description": "Cucumber salad made with cucumber and vinegar"},
        {"fdcId": 21, "dataType": "Survey (FNDDS)", "description": "Cucumber, raw"},
    ]
    relevance = [{"fdcId": 22, "dataType": "Branded", "description": "CUCUMBER"}]
    merged = _merge_usda(generic, relevance, "cucumber", 10)
    assert merged[0]["description"] == "Cucumber, raw"


def test_merge_usda_keeps_brand_first_for_brand_query():
    """'dr pepper' must not be hijacked by a generic 'Pepper steak' match."""
    from app.services.food_lookup import _merge_usda
    generic = [
        {"fdcId": 10, "dataType": "Survey (FNDDS)", "description": "Pepper steak"},
        {"fdcId": 11, "dataType": "Survey (FNDDS)", "description": "Peppers, jalapenos"},
    ]
    relevance = [{"fdcId": 12, "dataType": "Branded", "description": "DR PEPPER"}]
    merged = _merge_usda(generic, relevance, "dr pepper", 10)
    assert merged[0]["description"] == "DR PEPPER"


def test_weblookup_requires_auth(client):
    r = client.post("/api/foods/weblookup", json={"name": "burrito", "brand": "chipotle"})
    assert r.status_code == 401


def test_weblookup_returns_draft(client, monkeypatch):
    from app.routers import foods

    async def fake_lookup(name, brand, uid):
        return {"found": True, "name": "Chipotle Steak Burrito", "serving": "1 burrito",
                "calories": 920, "protein_g": 41, "carbs_g": 98, "fat_g": 38,
                "source_url": "https://chipotle.com"}

    monkeypatch.setattr(foods.ai, "lookup_nutrition_web", fake_lookup)
    monkeypatch.setattr(foods, "ANTHROPIC_API_KEY", "test-key")
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/foods/weblookup", json={"name": "steak burrito", "brand": "chipotle"})
    assert r.status_code == 200
    assert r.json()["calories"] == 920


def test_weblookup_no_api_key_503(client, monkeypatch):
    from app.routers import foods
    monkeypatch.setattr(foods, "ANTHROPIC_API_KEY", "")
    client.post("/api/auth/register", json=REG)
    assert client.post("/api/foods/weblookup", json={"name": "x", "brand": "y"}).status_code == 503


def test_serving_grams_conversion():
    from app.services.food_lookup import _serving_grams
    assert _serving_grams(355, "ml") == 355.0
    assert _serving_grams(34, "GRM") == 34.0
    assert _serving_grams(2, "ONZ") == pytest.approx(56.7, abs=0.1)
    assert _serving_grams(None, "g") is None
    assert _serving_grams(100, "WEIRD") is None


def test_external_search_carries_serving_g(client, monkeypatch):
    """A branded food's serving size should survive cache + retrieval."""
    import json
    from app.services import food_lookup

    async def fake_usda(query, limit):
        n = {"calories": 41.0, "protein_g": 0.0, "carbs_g": 11.0, "fat_g": 0.0, "fiber_g": 0.0, "micros": {}}
        return [{
            "source": "usda", "source_id": "DRP1", "name": "Dr Pepper",
            "brand": "Dr Pepper", "serving_desc": "1 can", "serving_g": 360.0,
            "nutrients_json": json.dumps(n),
        }]

    monkeypatch.setattr(food_lookup, "_search_usda", fake_usda)
    client.post("/api/auth/register", json=REG)
    food = client.get("/api/foods/search?q=dr+pepper").json()[0]
    assert food["serving_g"] == 360.0
    assert food["serving_desc"] == "1 can"


def test_external_search_has_local_shape(client, monkeypatch):
    """A cache-miss search must cache + return the same shape as a local hit
    (id + nutrients_per_100g), so the result is immediately loggable."""
    import json
    from app.services import food_lookup

    async def fake_usda(query, limit):
        nutrients = {"calories": 50.0, "protein_g": 1.0, "carbs_g": 12.0,
                     "fat_g": 0.2, "fiber_g": 2.0, "micros": {}}
        return [{
            "source": "usda", "source_id": "APPLE1", "name": "Apple",
            "brand": None, "serving_desc": None,
            "nutrients_json": json.dumps(nutrients),
        }]

    monkeypatch.setattr(food_lookup, "_search_usda", fake_usda)

    client.post("/api/auth/register", json=REG)
    r = client.get("/api/foods/search?q=apple")
    assert r.status_code == 200
    food = r.json()[0]
    assert isinstance(food["id"], int)
    assert food["nutrients_per_100g"]["calories"] == 50.0

    # And it is immediately loggable by that id
    log = client.post("/api/log/", json={"food_id": food["id"], "quantity_g": 200.0})
    assert log.status_code == 200
    assert log.json()["calories"] == 100.0
