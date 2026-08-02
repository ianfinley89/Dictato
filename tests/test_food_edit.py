"""Correcting what the AI got wrong, and getting out of a bad capture.

From a real log: Whisper heard "Kodiak" as "Kodak", so the web lookup found the
right product but saved it under the wrong name — and there was no way to fix the
name, fix the numbers, or abandon the capture.
"""
import json

import pytest

from app.database import get_conn

REG = {"email": "edit@example.com", "password": "password123", "display_name": "E"}


def _register(client) -> int:
    return client.post("/api/auth/register", json=REG).json()["user_id"]


def _food(source="web", name="kodak protein pancakes", uid=None, cal=358.0) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, name, brand, nutrients_json, created_by_user_id)
               VALUES (?,?,?,?,?)""",
            (source, name, "kodak",
             json.dumps({"calories": cal, "protein_g": 26.4, "carbs_g": 37.7,
                         "fat_g": 5.7, "fiber_g": 3.0}), uid))
        return cur.lastrowid


# ── Editing a food the AI made for you ───────────────────────────────────────
def test_fix_a_misheard_name(client):
    uid = _register(client)
    fid = _food(uid=uid)
    r = client.put(f"/api/foods/{fid}", json={"name": "Kodiak protein pancakes",
                                              "brand": "Kodiak Cakes"})
    assert r.status_code == 200
    assert r.json()["name"] == "kodiak protein pancakes"
    assert r.json()["brand"] == "kodiak cakes"


def test_fix_nutrition_from_the_package(client):
    uid = _register(client)
    fid = _food(uid=uid)
    r = client.put(f"/api/foods/{fid}", json={"calories": 300, "protein_g": 30,
                                              "carbs_g": 35, "fat_g": 4})
    assert r.status_code == 200
    assert r.json()["nutrients_per_100g"]["calories"] == pytest.approx(300.0)
    assert r.json()["nutrients_per_100g"]["protein_g"] == pytest.approx(30.0)


def test_per_serving_numbers_are_converted(client):
    """Labels publish per serving; the user shouldn't do arithmetic."""
    uid = _register(client)
    fid = _food(uid=uid)
    r = client.put(f"/api/foods/{fid}", json={"values_per": "serving", "serving_g": 50,
                                              "calories": 190})
    assert r.json()["nutrients_per_100g"]["calories"] == pytest.approx(380.0)


def test_shared_reference_data_cannot_be_rewritten(client):
    """One user must not be able to edit the USDA row everyone else sees."""
    _register(client)
    for source in ("usda", "off", "fatsecret"):
        fid = _food(source=source, name=f"{source} thing")
        r = client.put(f"/api/foods/{fid}", json={"name": "hijacked"})
        assert r.status_code == 403, source


def test_cannot_edit_someone_elses_food(client):
    _register(client)
    other = client.post("/api/auth/register", json={
        "email": "other@example.com", "password": "password123", "display_name": "O"}
    ).json()["user_id"]
    fid = _food(source="estimate", uid=other)
    client.post("/api/auth/login", json={"email": REG["email"], "password": REG["password"]})
    assert client.put(f"/api/foods/{fid}", json={"name": "mine now"}).status_code == 404


def test_edited_nutrition_can_refresh_the_entry_that_prompted_it(client):
    """Snapshots normally freeze at log time, but the entry you were LOOKING at
    when you fixed the food should reflect the fix."""
    uid = _register(client)
    fid = _food(uid=uid, cal=358.0)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 100,
                                       "source": "voice"}).json()
    assert e["calories"] == pytest.approx(358.0)
    client.put(f"/api/foods/{fid}", json={"calories": 300, "resync_entry_id": e["id"]})
    with get_conn() as conn:
        snap = json.loads(conn.execute(
            "SELECT nutrients_snapshot_json FROM log_entries WHERE id=?",
            (e["id"],)).fetchone()["nutrients_snapshot_json"])
    assert snap["calories"] == pytest.approx(300.0)


def test_resync_does_not_claim_the_user_set_the_portion(client):
    """They corrected the FOOD, not the portion — polluting the personal prior
    with an unchosen quantity would be a lie."""
    uid = _register(client)
    fid = _food(uid=uid)
    e = client.post("/api/log/", json={"food_id": fid, "quantity_g": 100,
                                       "source": "voice"}).json()
    client.put(f"/api/foods/{fid}", json={"calories": 300, "resync_entry_id": e["id"]})
    with get_conn() as conn:
        row = conn.execute("SELECT portion_manual FROM log_entries WHERE id=?",
                           (e["id"],)).fetchone()
    assert row["portion_manual"] == 0


# ── Getting out of a bad capture ─────────────────────────────────────────────
def test_discard_capture_removes_every_entry(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _seed_food, _script_llm, _tool, _text, _knows_food
    uid = _register(client)
    a = _seed_food()
    b = _seed_food(name="berries", serving_g=None)
    _knows_food(uid, a); _knows_food(uid, b)
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": 1, "basis": "count", "servings": 2, "quantity_g": 18}, "t1"),
        _tool("log_food", {"food_id": 2, "basis": "estimate", "quantity_g": 100}, "t2"),
        _text("Logged them."),
    ])
    r = client.post("/api/agent/log", data={"text": "pancakes and berries"})
    cap_id = r.json()["capture_id"]
    assert len(r.json()["entries"]) == 2

    d = client.delete(f"/api/agent/capture/{cap_id}")
    assert d.status_code == 200 and d.json()["removed"] == 2
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM log_entries WHERE user_id=?",
                            (uid,)).fetchone()["c"] == 0
        # The capture itself survives, tagged — a rejected capture is training signal.
        row = conn.execute("SELECT transcript, tags_json FROM capture_log WHERE id=?",
                           (cap_id,)).fetchone()
    assert row["transcript"] == "pancakes and berries"
    assert "correction:discarded" in json.loads(row["tags_json"])


def test_cannot_discard_someone_elses_capture(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _seed_food, _script_llm, _tool, _text, _knows_food
    uid = _register(client)
    _seed_food()
    _knows_food(uid, 1)
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": 1, "basis": "count", "servings": 1, "quantity_g": 9}, "t1"),
        _text("Logged."),
    ])
    cap_id = client.post("/api/agent/log", data={"text": "a rice cake"}).json()["capture_id"]
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": "thief@example.com",
                                            "password": "password123", "display_name": "T"})
    assert client.delete(f"/api/agent/capture/{cap_id}").status_code == 403


# ── The training label must reflect what the user ended up with ──────────────
def _export(conn_free=True) -> list:
    import io, json as _json
    from scripts.export_dataset import export
    buf = io.StringIO()
    export(buf)
    return [_json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


def test_a_rename_reaches_the_training_label(client, monkeypatch):
    """"kodak" -> "kodiak" is exactly the signal worth training on; reading the
    frozen snapshot name would throw it away."""
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _seed_food, _script_llm, _tool, _text, _knows_food
    uid = _register(client)
    fid = _seed_food(name="kodak protein pancakes", serving_g=124.0)
    _knows_food(uid, fid)
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": fid, "basis": "count", "servings": 1,
                           "quantity_g": 124}, "t1"),
        _text("Logged the pancakes."),
    ])
    client.post("/api/agent/log", data={"text": "kodak protein pancakes"})
    with get_conn() as conn:
        conn.execute("UPDATE foods SET name='kodiak protein pancakes' WHERE id=?", (fid,))

    item = _export()[0]["items"][0]
    assert item["food_name"] == "kodiak protein pancakes"
    assert item["model_food_name"] == "kodak protein pancakes"


def test_an_undone_item_stays_in_the_example_as_a_negative(client, monkeypatch):
    """The model's original guess IS the input; deleting it would destroy the
    negative example. It must survive, labelled kept=False."""
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _seed_food, _script_llm, _tool, _text, _knows_food
    uid = _register(client)
    fid = _seed_food(name="edamame, frozen", serving_g=None)
    _knows_food(uid, fid)
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": fid, "basis": "estimate", "quantity_g": 45}, "t1"),
        _text("Logged it."),
    ])
    entry_id = client.post("/api/agent/log",
                           data={"text": "some greens"}).json()["entries"][0]["id"]
    client.delete(f"/api/log/{entry_id}")

    item = _export()[0]["items"][0]
    assert item["food_name"] == "edamame, frozen"     # still present
    assert item["kept"] is False                      # but labelled as removed


def test_a_portion_fix_is_marked_as_user_set(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _seed_food, _script_llm, _tool, _text, _knows_food
    uid = _register(client)
    fid = _seed_food(name="rice cake", serving_g=9.0)
    _knows_food(uid, fid)
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": fid, "basis": "estimate", "quantity_g": 100}, "t1"),
        _text("Logged it."),
    ])
    entry_id = client.post("/api/agent/log",
                           data={"text": "a snack"}).json()["entries"][0]["id"]
    client.put(f"/api/log/{entry_id}/portion", json={"quantity_g": 27})

    item = _export()[0]["items"][0]
    assert item["kept"] is True
    assert item["quantity_g"] == pytest.approx(27.0)   # the corrected amount
    assert item["portion_corrected"] is True           # and that a human set it
