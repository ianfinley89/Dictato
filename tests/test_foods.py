import json
import pytest
from app.database import get_conn

REG = {"email": "bob@example.com", "password": "password123", "display_name": "Bob"}


def _seed_food(name: str = "Test Rice Cake", calories: float = 390.0,
               brand: str | None = None) -> int:
    nutrients = {
        "calories": calories, "protein_g": 8.0, "carbs_g": 80.0, "fat_g": 2.0,
        "fiber_g": 1.0, "micros": {},
    }
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, brand, nutrients_json) VALUES (?,?,?,?,?)",
            ("manual", None, name, brand, json.dumps(nutrients)),
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


def test_off_parser_converts_kj_energy():
    """Open Food Facts energy fallbacks are kilojoules — the parser must convert."""
    from app.services.food_lookup import _parse_off
    # Only the kJ field present (the ground-beef bug shape).
    kj_only = _parse_off({
        "product_name": "Ground Beef",
        "nutriments": {"energy_100g": 1046, "proteins_100g": 17,
                       "carbohydrates_100g": 0, "fat_100g": 20},
    })
    assert json.loads(kj_only["nutrients_json"])["calories"] == pytest.approx(250.0, abs=1)
    # kcal field present → used as-is.
    kcal = _parse_off({
        "product_name": "Ground Beef",
        "nutriments": {"energy-kcal_100g": 254, "energy_100g": 1046, "proteins_100g": 17},
    })
    assert json.loads(kcal["nutrients_json"])["calories"] == pytest.approx(254.0)


def test_cache_guard_fixes_kj_energy(client, monkeypatch):
    """A source that hands _cache_food raw kJ-as-kcal gets corrected on store."""
    from app.services import food_lookup

    async def fake_usda(query, limit):
        n = {"calories": 1046.0, "protein_g": 17.0, "carbs_g": 0.0,
             "fat_g": 20.0, "fiber_g": 0.0, "micros": {}}
        return [{"source": "usda", "source_id": "BEEF1", "name": "Ground Beef",
                 "brand": None, "serving_desc": None, "serving_g": None,
                 "nutrients_json": json.dumps(n)}]

    monkeypatch.setattr(food_lookup, "_search_usda", fake_usda)
    client.post("/api/auth/register", json=REG)
    food = client.get("/api/foods/search?q=ground+beef").json()[0]
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(250.0, abs=2)


def test_repair_nutrients_fixes_existing_rows():
    """The startup repair rewrites rows poisoned before the guard existed."""
    from app.database import get_conn, _repair_nutrients
    bad = {"calories": 1046.0, "protein_g": 17.0, "carbs_g": 0.0, "fat_g": 20.0,
           "fiber_g": 0.0, "micros": {}}
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO foods (source, source_id, name, nutrients_json) VALUES (?,?,?,?)",
            ("off", "BEEF-KJ", "Ground Beef", json.dumps(bad)),
        )
        fid = cur.lastrowid
        _repair_nutrients(conn)
        fixed = json.loads(conn.execute(
            "SELECT nutrients_json FROM foods WHERE id=?", (fid,)).fetchone()["nutrients_json"])
    assert fixed["calories"] == pytest.approx(250.0, abs=2)


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


# ── Zero-calorie foods vs rows with no data at all ───────────────────────────
# Open Food Facts contains non-food submissions (a 9V battery reached our cache).
# Filtering "0 calories" would have thrown away Coke Zero, sparkling water and
# black coffee — the test is whether nutrition was MEASURED, not whether it's 0.

def test_genuine_zero_calorie_products_are_kept():
    from app.services.food_lookup import _parse_off
    coke_zero = {
        "product_name": "Coca-Cola Zero",
        "brands": "Coca-Cola",
        "nutriments": {"energy-kcal_100g": 0.2, "proteins_100g": 0,
                       "carbohydrates_100g": 0, "fat_100g": 0},
    }
    parsed = _parse_off(coke_zero)
    assert parsed is not None
    import json as _json
    assert _json.loads(parsed["nutrients_json"])["calories"] == 0.2


def test_explicit_all_zero_food_is_kept():
    """Water and unsweetened tea are genuinely 0 across the board."""
    from app.services.food_lookup import _parse_off
    water = {"product_name": "Sparkling Water", "brands": "x",
             "nutriments": {"energy-kcal_100g": 0, "proteins_100g": 0,
                            "carbohydrates_100g": 0, "fat_100g": 0}}
    assert _parse_off(water) is not None


def test_rows_with_no_nutrition_data_are_rejected():
    """The battery: nutrient fields absent entirely, not zero."""
    from app.services.food_lookup import _parse_off
    battery = {"product_name": "Pila 9V", "brands": "Kodak", "nutriments": {}}
    assert _parse_off(battery) is None
    assert _parse_off({"product_name": "Kodak", "brands": "Kodak"}) is None


def test_partial_nutrition_still_counts_as_data():
    from app.services.food_lookup import _parse_off
    only_energy = {"product_name": "Mystery Drink", "brands": "x",
                   "nutriments": {"energy-kcal_100g": 12}}
    assert _parse_off(only_energy) is not None


# ── Search routing: a source returning rows != a source finding the food ─────
# Real failure: searching "Kodiak protein pancakes" returned Chinese Pancake,
# Pancake Syrup and Pancakes Chocolate. USDA matched "pancakes", ignored
# "kodiak", and because its list was non-empty the loop stopped — so FatSecret,
# which holds the real Kodiak Cakes products, was never queried.

def test_coverage_measures_the_whole_query_not_each_result():
    from app.services.food_lookup import relevance_score
    generic = [{"name": "Pancakes, Nfs"}, {"name": "Pancake Syrup"}]
    assert relevance_score(generic, "kodiak protein pancakes") == pytest.approx(0.5)
    both = [{"name": "Power Cakes pancakes", "brand": "Kodiak Cakes"}]
    assert relevance_score(both, "kodiak protein pancakes") == pytest.approx(1.0)


def test_common_food_words_do_not_prove_relevance():
    """"protein" is too common to identify anything, so it isn't a query token."""
    from app.services.food_lookup import _query_tokens
    assert _query_tokens("Kodiak protein pancakes") == ["kodiak", "pancakes"]
    assert _query_tokens("the fresh raw whole food") == []


def test_brand_match_breaks_the_tie():
    """USDA covers 'pancakes', FatSecret covers 'kodiak' — both 0.5. The brand is
    what the user actually named, so it decides."""
    from app.services.food_lookup import source_score
    usda = [{"name": "Pancakes, Nfs", "brand": None}]
    fatsecret = [{"name": "Protein Oats", "brand": "Kodiak Cakes"}]
    assert source_score(fatsecret, "kodiak protein pancakes") > \
           source_score(usda, "kodiak protein pancakes")


def test_empty_results_score_zero_and_never_win():
    from app.services.food_lookup import source_score
    assert source_score([], "anything") == (0.0, 0.0)


def test_ranking_puts_fuller_matches_first():
    from app.services.food_lookup import _rank_by_relevance
    foods = [{"name": "Pancake Syrup", "brand": None},
             {"name": "Power Cakes pancakes", "brand": "Kodiak Cakes"}]
    assert _rank_by_relevance(foods, "kodiak pancakes")[0]["brand"] == "Kodiak Cakes"


def test_a_query_with_no_distinctive_words_is_always_satisfied():
    """Prevents an endless fan-out on a query made only of filler."""
    from app.services.food_lookup import relevance_score
    assert relevance_score([{"name": "anything"}], "the raw whole") == 1.0


# ── Full-text local search ───────────────────────────────────────────────────
# `LIKE %whole query%` cannot match a multi-word search: "oberto beef jerky"
# found nothing locally even though "Beef Jerky" by Oberto was cached, so every
# such search paid for an external lookup.

def test_multi_word_search_matches_cached_food(client):
    from app.services.food_lookup import _search_local
    client.post("/api/auth/register", json=REG)
    _seed_food("Beef Jerky", brand="Oberto Sausage Company")
    hits = _search_local("oberto beef jerky", 1, 5)
    assert any("jerky" in h["name"].lower() for h in hits)


def test_word_order_does_not_matter(client):
    from app.services.food_lookup import _search_local
    client.post("/api/auth/register", json=REG)
    _seed_food("Vietnamese Chicken Glass Noodle Soup")
    assert _search_local("chicken soup vietnamese", 1, 5)


def test_index_follows_renames_and_deletes(client):
    """The triggers must keep the index in step, or a corrected food stays
    findable only under its old name."""
    from app.database import get_conn
    from app.services.food_lookup import _search_local
    client.post("/api/auth/register", json=REG)
    fid = _seed_food("kodak protein pancakes")
    assert _search_local("kodak", 1, 5)
    with get_conn() as conn:
        conn.execute("UPDATE foods SET name='kodiak protein pancakes' WHERE id=?", (fid,))
    assert _search_local("kodiak", 1, 5)
    with get_conn() as conn:
        conn.execute("DELETE FROM foods WHERE id=?", (fid,))
    assert not _search_local("kodiak", 1, 5)


def test_search_survives_a_missing_index(client, monkeypatch):
    """If the index is unavailable the LIKE path must still answer."""
    from app.services import food_lookup
    client.post("/api/auth/register", json=REG)
    _seed_food("Plain Rice Cake")
    monkeypatch.setattr(food_lookup, "_fts_query", lambda q: None)
    assert food_lookup._search_local("Plain Rice Cake", 1, 5)


# ── USDA energy units ────────────────────────────────────────────────────────
def test_usda_energy_reads_kcal_not_kj():
    """SR Legacy rows carry TWO nutrients both named "Energy" — kcal and kJ.
    Keyed by name alone the kJ row wins by arriving last, and a raw carrot was
    cached at 173 kcal/100g instead of 41 (fdc 170393, verified against the
    live API)."""
    from app.services.food_lookup import _parse_usda
    food = _parse_usda({
        "fdcId": 170393, "description": "Carrots, raw", "dataType": "SR Legacy",
        "foodNutrients": [
            {"nutrientName": "Energy", "unitName": "kcal", "value": 41.0},
            {"nutrientName": "Energy", "unitName": "kJ", "value": 173.0},
            {"nutrientName": "Protein", "unitName": "G", "value": 0.93},
            {"nutrientName": "Carbohydrate, by difference", "unitName": "G", "value": 9.58},
            {"nutrientName": "Total lipid (fat)", "unitName": "G", "value": 0.24},
        ]})
    assert json.loads(food["nutrients_json"])["calories"] == 41.0


def test_usda_energy_survives_a_missing_unit():
    """Older payloads omit unitName; an energy figure with no unit is still the
    only one we have, so keep it rather than zeroing the food out."""
    from app.services.food_lookup import _parse_usda
    food = _parse_usda({
        "fdcId": 1, "description": "Thing",
        "foodNutrients": [{"nutrientName": "Energy", "value": 88.0}]})
    assert json.loads(food["nutrients_json"])["calories"] == 88.0
