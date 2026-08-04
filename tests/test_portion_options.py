"""The portion picker: fix a portion by tapping a real measure.

Every gram figure offered must come from USDA data, the food's own serving size,
or the user's own history — never from a model. And choosing one must count as
the user setting it, so the same food stops being guessed next time.
"""
import json

import pytest

from app.database import get_conn
from app.services.portion import build_options

REG = {"email": "portion@example.com", "password": "password123", "display_name": "P"}

PICKLE_PORTIONS = [
    {"unit": "medium", "qty": 1, "grams": 65.0, "desc": "1 medium (3-3/4\" long)"},
    {"unit": "large", "qty": 1, "grams": 135.0, "desc": "1 large (4\" long)"},
    {"unit": "spear", "qty": 1, "grams": 30.0, "desc": "1 spear"},
    {"unit": "slice", "qty": 1, "grams": 7.0, "desc": "1 slice"},
]


def _register(client) -> int:
    return client.post("/api/auth/register", json=REG).json()["user_id"]


def _food(name="pickles", serving_g=None, portions=None, source_id=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, source_id, name, serving_desc, serving_g,
                                  portions_json, nutrients_json)
               VALUES ('usda', ?, ?, ?, ?, ?, ?)""",
            (source_id, name, "1 cake" if serving_g else None, serving_g,
             json.dumps(portions) if portions else None,
             json.dumps({"calories": 100.0, "protein_g": 5.0, "carbs_g": 10.0, "fat_g": 2.0})))
        return cur.lastrowid


# ── Building the options ─────────────────────────────────────────────────────
def test_usda_household_measures_become_options():
    opts = build_options({"portions": PICKLE_PORTIONS}, current_g=100)
    labels = [o["label"] for o in opts]
    assert "1 spear" in labels and "1 medium (3-3/4\" long)" in labels
    spear = next(o for o in opts if o["label"] == "1 spear")
    assert spear["grams"] == 30.0
    assert spear["basis"] == "household" and spear["household_unit"] == "spear"
    assert [o["grams"] for o in opts] == sorted(o["grams"] for o in opts)   # ascending


def test_serving_size_gives_half_one_and_double():
    opts = build_options({"serving_g": 11.0, "serving_desc": "1 cake"}, current_g=100)
    grams = sorted(o["grams"] for o in opts)
    assert grams == [5.5, 11.0, 22.0]
    assert all(o["basis"] == "count" for o in opts)
    assert next(o for o in opts if o["grams"] == 5.5)["servings"] == 0.5


def test_personal_prior_is_offered():
    opts = build_options({"serving_g": 11.0, "serving_desc": "1 cake"}, current_g=100,
                         prior={"grams": 33.0, "n": 4, "cv": 0.02, "kind": "verified"})
    usual = next(o for o in opts if o["label"] == "your usual")
    assert usual["grams"] == 33.0 and usual["basis"] == "history"


def test_foods_that_know_nothing_still_offer_something():
    """Generic FNDDS rows (60% of what users log) have no serving_g and no USDA
    portions — scaling what was logged is better than an empty menu."""
    opts = build_options({}, current_g=200)
    assert sorted(o["grams"] for o in opts) == [100.0, 200.0, 400.0]


def test_multiples_scale_from_the_real_measure_not_the_guess():
    """With one real measure ("1 cup" = 150g), the extra choices must be half and
    two CUPS — not half of whatever the model happened to guess."""
    kimchi = {"portions": [{"unit": "cup", "qty": 1, "grams": 150.0, "desc": "1 cup"}]}
    grams = sorted(o["grams"] for o in build_options(kimchi, current_g=200))
    assert grams == [75.0, 150.0, 300.0]      # not 100/200/400


def test_halves_and_doubles_come_off_the_largest_measure():
    """USDA lists "1 fl oz" alongside "1 can or bottle (12 fl oz)"; a portion of
    beer is a fraction of the CAN, not of a fluid ounce."""
    beer = {"portions": [
        {"unit": "fl oz", "qty": 1, "grams": 29.5, "desc": "1 fl oz"},
        {"unit": "can or bottle", "qty": 1, "grams": 354.0,
         "desc": "1 can or bottle (12 fl oz)"}]}
    opts = build_options(beer, current_g=200)
    assert any(abs(o["grams"] - 177.0) < 0.5 for o in opts)   # half a can
    assert any(abs(o["grams"] - 708.0) < 0.5 for o in opts)   # two cans


def test_current_amount_is_marked():
    opts = build_options({"portions": PICKLE_PORTIONS}, current_g=65)
    assert next(o for o in opts if o["grams"] == 65.0)["current"] is True
    assert next(o for o in opts if o["grams"] == 30.0)["current"] is False


def test_bulk_containers_and_yields_are_never_offered():
    """USDA lists "1 large pot (60 FO, 12 servings)" for coffee. Offering that as
    a portion invites a catastrophic mistap; a single-serving container must
    still survive the same filter."""
    coffee = {"portions": [
        {"unit": "fl oz", "qty": 1, "grams": 30.0, "desc": "1 fl oz"},
        {"unit": "medium", "qty": 1, "grams": 480.0, "desc": "1 medium"},
        {"unit": "large pot", "qty": 1, "grams": 1800.0,
         "desc": "1 large pot (60 FO, 12 servings)"}]}
    labels = [o["label"] for o in build_options(coffee, current_g=240)]
    assert "1 medium" in labels
    assert not any("pot" in l for l in labels)
    assert all(o["grams"] <= 1000 for o in build_options(coffee, current_g=240))

    yielded = {"portions": [{"unit": "oz", "qty": 1, "grams": 20.0, "desc": "1 oz yields"}]}
    assert not any("yields" in o["label"] for o in build_options(yielded, current_g=100))

    keep = {"portions": [{"unit": "container", "qty": 1, "grams": 150.0,
                          "desc": "1 single serving container"}]}
    assert "1 single serving container" in [o["label"] for o in build_options(keep, current_g=200)]


def test_duplicate_and_absurd_options_are_dropped():
    dup = [{"unit": "cup", "qty": 1, "grams": 11.0, "desc": "1 cup"}]
    opts = build_options({"serving_g": 11.0, "serving_desc": "1 cake", "portions": dup},
                         current_g=11)
    assert sum(1 for o in opts if abs(o["grams"] - 11.0) < 0.5) == 1
    huge = [{"unit": "sack", "qty": 1, "grams": 99999.0, "desc": "1 sack"}]
    assert all(o["grams"] <= 2500 for o in build_options({"portions": huge}, current_g=50))


# ── Endpoints ────────────────────────────────────────────────────────────────
def test_options_endpoint_returns_choices(client):
    uid = _register(client)
    fid = _food(portions=PICKLE_PORTIONS)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 100,
                                       "source": "manual"}).json()
    r = client.get(f"/api/log/{e['id']}/portions")
    assert r.status_code == 200
    assert "1 spear" in [o["label"] for o in r.json()["options"]]


def test_picking_a_portion_updates_and_is_remembered(client):
    """The headline: choose "1 spear" and the entry becomes 30g — and because the
    user chose it, the personal prior treats it as verified next time."""
    uid = _register(client)
    fid = _food(portions=PICKLE_PORTIONS)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 250,
                                       "source": "voice"}).json()
    r = client.put(f"/api/log/{e['id']}/portion", json={"quantity_g": 30, "basis": "household"})
    assert r.status_code == 200
    assert r.json()["quantity_g"] == pytest.approx(30.0)
    assert r.json()["calories"] == pytest.approx(30.0)      # snapshot recomputed

    with get_conn() as conn:
        row = conn.execute("SELECT source, portion_manual FROM log_entries WHERE id=?",
                           (e["id"],)).fetchone()
    assert row["source"] == "voice"          # capture method preserved
    assert row["portion_manual"] == 1        # but the portion is now user-set

    from app.services.portion_history import personal_prior
    p = personal_prior(uid, fid)
    assert p["kind"] == "verified" and p["grams"] == pytest.approx(30.0)


def test_portion_endpoints_are_scoped_to_the_owner(client):
    _register(client)
    fid = _food(portions=PICKLE_PORTIONS)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 100,
                                       "source": "manual"}).json()
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": "other@example.com",
                                           "password": "password123", "display_name": "O"})
    assert client.get(f"/api/log/{e['id']}/portions").status_code == 403
    assert client.put(f"/api/log/{e['id']}/portion", json={"quantity_g": 30}).status_code == 403


def test_absurd_picked_portion_is_clamped(client):
    _register(client)
    fid = _food(serving_g=11.0)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 11,
                                       "source": "manual"}).json()
    r = client.put(f"/api/log/{e['id']}/portion", json={"quantity_g": 99999})
    assert r.status_code == 200
    assert r.json()["quantity_g"] <= 2500


def test_a_food_that_knows_nothing_still_offers_a_real_serving():
    """Half of logged foods have no serving size and no USDA measure. What a
    serving of the KIND weighs is a database fact, and offering it as a choice is
    safe where silently clamping the logged amount was not."""
    opts = build_options({}, current_g=200,
                         class_typical={"grams": 150.0, "class": "yogurt"})
    labels = [o["label"] for o in opts]
    assert "typical yogurt" in labels
    assert next(o for o in opts if o["label"] == "typical yogurt")["grams"] == 150.0


def test_the_foods_own_measures_still_come_first():
    opts = build_options({"portions": PICKLE_PORTIONS}, current_g=100,
                         class_typical={"grams": 500.0, "class": "vegetable"})
    assert "1 spear" in [o["label"] for o in opts]


# ── Backfilling USDA measures for foods people actually logged ───────────────
def test_backfill_only_touches_logged_or_favourited_usda_foods(client, monkeypatch):
    """One network call per food and a ~1000/hour USDA cap, so it must not walk
    the whole cache — only the foods that need an anchor."""
    import asyncio
    from app.services import food_lookup
    uid = _register(client)
    logged = _food(name="cheeseburger", source_id="1234")
    _food(name="never logged", source_id="9999")                      # must be skipped
    client.post("/api/log/", json={"food_id": logged, "quantity_g": 200,
                                   "source": "manual"})
    seen = []

    async def fake_ensure(food):
        seen.append(food["id"])
        with get_conn() as conn:
            conn.execute("UPDATE foods SET portions_json=? WHERE id=?",
                         (json.dumps([{"unit": "cheeseburger", "qty": 1,
                                       "grams": 210.0, "desc": "1 cheeseburger"}]),
                          food["id"]))
        return {**food, "portions": [{"unit": "cheeseburger", "qty": 1,
                                      "grams": 210.0, "desc": "1 cheeseburger"}]}

    monkeypatch.setattr(food_lookup, "ensure_portions", fake_ensure)
    monkeypatch.setattr(food_lookup, "USDA_API_KEY", "test-key")
    n = asyncio.run(food_lookup.backfill_portions(limit=10))
    assert seen == [logged] and n == 1


def test_backfill_does_not_refetch_what_it_already_has(client, monkeypatch):
    import asyncio
    from app.services import food_lookup
    uid = _register(client)
    fid = _food(name="pickles", portions=PICKLE_PORTIONS, source_id="4321")   # already has measures
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 100,
                                   "source": "manual"})
    monkeypatch.setattr(food_lookup, "USDA_API_KEY", "test-key")

    async def boom(food):
        raise AssertionError("should not refetch")

    monkeypatch.setattr(food_lookup, "ensure_portions", boom)
    assert asyncio.run(food_lookup.backfill_portions(limit=10)) == 0
