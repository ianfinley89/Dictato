"""Tests for the agentic logging endpoint: fast path, tool execution, and the
mocked model loop. No real Anthropic or Whisper calls."""
import io
import json

import pytest

from app.database import get_conn

REG = {"email": "agent@example.com", "password": "password123", "display_name": "A"}

# 1x1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _register(client):
    client.post("/api/auth/register", json=REG)
    with get_conn() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?", (REG["email"],)).fetchone()["id"]


def _seed_food(name="rice cake", serving_g=9.0, cal=387.0):
    nutrients = {"calories": cal, "protein_g": 8.0, "carbs_g": 82.0, "fat_g": 3.0, "fiber_g": 3.0}
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, name, serving_desc, serving_g, nutrients_json)
               VALUES ('usda', ?, '1 cake', ?, ?)""",
            (name, serving_g, json.dumps(nutrients)),
        )
        return cur.lastrowid


def _log_once(uid, food_id):
    from app.services.logging import log_entry_for_user
    return log_entry_for_user(uid, food_id, 9.0, "manual")


# ── Endpoint basics ───────────────────────────────────────────────────────────

def test_agent_log_requires_auth(client):
    r = client.post("/api/agent/log", data={"text": "an apple"})
    assert r.status_code == 401


def test_agent_log_requires_input(client):
    _register(client)
    r = client.post("/api/agent/log", data={})
    assert r.status_code == 400


def test_agent_log_rejects_non_image(client):
    _register(client)
    r = client.post("/api/agent/log", files={"image": ("n.txt", io.BytesIO(b"hi"), "text/plain")})
    assert r.status_code == 415


def test_agent_usage_endpoint(client):
    _register(client)
    r = client.get("/api/agent/usage")
    assert r.status_code == 200
    assert "vision_calls" in r.json()
    assert "daily_limit" in r.json()


def test_agent_503_without_api_key_when_no_fast_path(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "")
    _register(client)
    r = client.post("/api/agent/log", data={"text": "some exotic dish"})
    assert r.status_code == 503


def test_agent_rate_limited(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_LIMIT", "0")
    _register(client)
    r = client.post("/api/agent/log", data={"text": "some exotic dish"})
    assert r.status_code == 429


# ── Fast path ─────────────────────────────────────────────────────────────────

def test_fast_path_logs_known_food(client):
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)   # the user knows this food now

    r = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    assert r.status_code == 200
    data = r.json()
    assert data["fast_path"] is True
    assert len(data["entries"]) == 1
    e = data["entries"][0]
    assert e["food_name"] == "rice cake"
    assert e["quantity_g"] == pytest.approx(18.0)      # 2 servings x 9g
    assert e["food_source"] == "USDA"
    assert "rice cake" in data["summary"]


def test_fast_path_skips_unknown_food(client):
    uid = _register(client)
    _seed_food()   # exists in DB but user never logged/favorited it
    from app.services.agent import fast_path_log
    assert fast_path_log(uid, "a rice cake") is None


def test_fast_path_skips_counts_without_serving_size(client):
    """A count needs the food's real serving_g — defaulting to 100g/serving
    produced wildly wrong portions, so those go to the agent instead."""
    uid = _register(client)
    food_id = _seed_food(serving_g=None)
    _log_once(uid, food_id)
    from app.services.agent import fast_path_log
    assert fast_path_log(uid, "two rice cakes") is None


def test_fast_path_allows_explicit_grams_without_serving_size(client):
    uid = _register(client)
    food_id = _seed_food(serving_g=None)
    _log_once(uid, food_id)
    from app.services.agent import fast_path_log
    entries = fast_path_log(uid, "50g rice cake")
    assert entries and entries[0]["quantity_g"] == pytest.approx(50.0)


def test_fast_path_skips_branded_items(client):
    uid = _register(client)
    food_id = _seed_food(name="steak burrito")
    _log_once(uid, food_id)
    from app.services.agent import fast_path_log
    assert fast_path_log(uid, "steak burrito from chipotle") is None


def test_fast_path_other_users_history_does_not_leak(client):
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)
    from app.services.agent import fast_path_log
    assert fast_path_log(uid + 1, "a rice cake") is None


# ── Agent tools ───────────────────────────────────────────────────────────────

def test_tool_create_food_estimate(client):
    uid = _register(client)
    from app.services.agent import _tool_create_food
    out = _tool_create_food(uid, {
        "name": "Homemade Stew", "values_per": "100g", "calories": 95, "protein_g": 7,
        "carbs_g": 8, "fat_g": 3.5, "basis": "estimate", "serving_g": 350,
    })
    assert out["created"] and out["source"] == "estimate"
    from app.services.food_lookup import get_food_by_id
    food = get_food_by_id(out["food_id"])
    assert food["name"] == "homemade stew"
    assert food["created_by_user_id"] == uid
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(95.0)


def test_tool_create_food_converts_per_serving_values(client):
    """Labels publish per-serving numbers (60 cal per 11g cake); the DB stores
    per-100g. The server does the conversion so the model can't get it wrong."""
    uid = _register(client)
    from app.services.agent import _tool_create_food
    out = _tool_create_food(uid, {
        "name": "White Cheddar Rice Cakes", "brand": "Quaker",
        "values_per": "serving", "serving_g": 11,
        "calories": 60, "protein_g": 1, "carbs_g": 12, "fat_g": 1,
        "basis": "web", "source_url": "https://example.com",
    })
    from app.services.food_lookup import get_food_by_id
    food = get_food_by_id(out["food_id"])
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(545.45, abs=0.1)
    assert food["serving_g"] == pytest.approx(11.0)


def test_tool_create_food_per_serving_requires_serving_g(client):
    uid = _register(client)
    from app.services.agent import _tool_create_food
    out = _tool_create_food(uid, {
        "name": "Mystery Snack", "values_per": "serving",
        "calories": 60, "protein_g": 1, "carbs_g": 12, "fat_g": 1, "basis": "web",
    })
    assert "error" in out


def test_tool_create_food_web_is_public_and_sanitized(client):
    uid = _register(client)
    from app.services.agent import _tool_create_food
    out = _tool_create_food(uid, {
        "name": "Mega Burrito", "brand": "Chipotle", "values_per": "100g",
        "calories": 99999, "protein_g": 12, "carbs_g": 14, "fat_g": 9, "basis": "web",
        "source_url": "https://example.com/nutrition",
    })
    from app.services.food_lookup import get_food_by_id
    food = get_food_by_id(out["food_id"])
    assert food["source"] == "web"
    # Impossible energy with sane macros → recomputed from Atwater (4·12+4·14+9·9=185),
    # which beats the old crude 900 cap.
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(185.0)
    assert food["source_id"] == "https://example.com/nutrition"


def test_tool_log_food_servings_math(client):
    uid = _register(client)
    food_id = _seed_food(serving_g=9.0)
    from app.services.agent import _tool_log_food
    logged = []
    out = _tool_log_food(uid, {"food_id": food_id, "servings": 3}, "voice", "note", logged)
    assert out["logged"] and out["quantity_g"] == pytest.approx(27.0)
    assert len(logged) == 1


def test_tool_log_food_unknown_id_returns_error(client):
    uid = _register(client)
    from app.services.agent import _tool_log_food
    out = _tool_log_food(uid, {"food_id": 99999, "quantity_g": 100}, "voice", None, [])
    assert "error" in out


# ── Mocked model loop (patches the provider-neutral llm.chat) ─────────────────
from app.services.llm import LLMResponse, ToolCall


def _text(t):
    return LLMResponse(text=t, tool_calls=[], stop_reason="end",
                       input_tokens=10, output_tokens=5, raw={"role": "assistant", "content": t})


def _tool(name, inp, tid="t1"):
    return LLMResponse(text="", tool_calls=[ToolCall(tid, name, inp)], stop_reason="tool_use",
                       input_tokens=10, output_tokens=5, raw=None)


def _script_llm(monkeypatch, responses):
    """Make llm.chat return the scripted responses in order."""
    from app.services import llm
    seq = list(responses)

    async def fake_chat(**kwargs):
        return seq.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)


@pytest.fixture()
def scripted_agent(monkeypatch):
    """Scripted search→log→summary run through the mocked llm.chat."""
    def install(food_name="rice cake"):
        _script_llm(monkeypatch, [
            _tool("search_food_db", {"query": food_name}, "t1"),
            # The fake model logs food_id 1 — tests seed exactly one food.
            _tool("log_food", {"food_id": 1, "servings": 2}, "t2"),
            _text(f"Logged 2 {food_name}s for you!"),
        ])

    return install


def test_agent_loop_logs_and_summarizes(client, scripted_agent, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    _register(client)
    _seed_food()          # becomes food_id 1
    scripted_agent()

    r = client.post("/api/agent/log", data={"text": "I had two rice cakes from the pantry shelf"})
    assert r.status_code == 200
    data = r.json()
    assert data["fast_path"] is False
    assert len(data["entries"]) == 1
    assert data["entries"][0]["quantity_g"] == pytest.approx(18.0)
    assert data["entries"][0]["food_source"] == "USDA"
    assert data["summary"] == "Logged 2 rice cakes for you!"
    # the entry is really in the DB
    today = client.get("/api/log/today").json()
    assert len(today) == 1


def test_agent_loop_image_path(client, scripted_agent, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    _register(client)
    _seed_food()
    scripted_agent()

    r = client.post("/api/agent/log", files={"image": ("meal.png", io.BytesIO(_PNG), "image/png")})
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["source"] == "photo"


def test_agent_loop_survives_api_error_after_partial_log(client, monkeypatch):
    """If the API dies mid-session, already-logged entries are still returned."""
    from app.services import llm
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()

    calls = {"n": 0}

    async def dying_chat(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool("log_food", {"food_id": 1, "quantity_g": 50}, "t1")
        raise RuntimeError("api down")

    monkeypatch.setattr(llm, "chat", dying_chat)

    r = client.post("/api/agent/log", data={"text": "something novel"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 1
    assert "did get logged" in data["summary"]


# ── Voice upload path (STT stubbed) ──────────────────────────────────────────

def test_audio_upload_transcribes_then_fast_paths(client, monkeypatch):
    from app.routers import agent as agent_router

    async def fake_transcribe(blob):
        return "I had two rice cakes"

    monkeypatch.setattr(agent_router.stt, "transcribe", fake_transcribe)
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)

    r = client.post("/api/agent/log", files={"audio": ("a.webm", io.BytesIO(b"fakeaudio"), "audio/webm")})
    assert r.status_code == 200
    data = r.json()
    assert data["transcript"] == "I had two rice cakes"
    assert data["fast_path"] is True
    assert data["entries"][0]["quantity_g"] == pytest.approx(18.0)


def test_audio_upload_undecodable_422(client, monkeypatch):
    from app.routers import agent as agent_router

    async def broken_transcribe(blob):
        raise ValueError("bad audio")

    monkeypatch.setattr(agent_router.stt, "transcribe", broken_transcribe)
    _register(client)
    r = client.post("/api/agent/log", files={"audio": ("a.webm", io.BytesIO(b"junk"), "audio/webm")})
    assert r.status_code == 422


# ── Capture log (raw voice/photo/text kept for later analysis) ────────────────

def test_capture_log_records_fast_path(client):
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)

    client.post("/api/agent/log", data={"text": "I had two rice cakes"})

    from app.database import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM capture_log WHERE user_id=?", (uid,)).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["input_type"] == "text"
    assert r["transcript"] == "I had two rice cakes"
    assert r["fast_path"] == 1
    entries = json.loads(r["entries_json"])
    assert entries and entries[0]["food_name"] == "rice cake"


def test_capture_log_records_voice_input_type(client, monkeypatch):
    from app.routers import agent as agent_router

    async def fake_transcribe(blob):
        return "I had two rice cakes"

    monkeypatch.setattr(agent_router.stt, "transcribe", fake_transcribe)
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)

    client.post("/api/agent/log", files={"audio": ("a.webm", io.BytesIO(b"x"), "audio/webm")})

    from app.database import get_conn
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert r["input_type"] == "voice"
    assert r["transcript"] == "I had two rice cakes"


# ── Capture annotations (tags, meal labels, specificity, passive facts) ───────

def test_agent_annotation_stored_and_facts_merged(client, monkeypatch):
    """The annotate_capture tool labels the capture and quietly updates the profile."""
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()

    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": 1, "servings": 2}, "t1"),
        _tool("annotate_capture", {
            "meal": "snack", "meal_label": "rice cakes", "tags": ["context:post-workout"],
            "specificity": "medium", "observed_facts": {"rides_bike": True},
        }, "t2"),
        _text("Logged 2 rice cakes!"),
    ])

    r = client.post("/api/agent/log", data={"text": "two rice cakes after I rode my bike home"})
    assert r.status_code == 200
    a = r.json()["annotation"]
    assert a["meal"] == "snack"
    assert a["meal_label"] == "rice cakes"
    assert a["tags"] == ["context:post-workout"]
    assert a["specificity"] == "medium"

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert row["meal"] == "snack"
    assert row["meal_label"] == "rice cakes"
    assert json.loads(row["tags_json"]) == ["context:post-workout"]
    assert row["specificity"] == "medium"

    from app.services.profile import get_profile
    assert get_profile(uid)["rides_bike"] is True


def test_fast_path_annotation_shape(client):
    uid = _register(client)
    food_id = _seed_food()
    _log_once(uid, food_id)

    r = client.post("/api/agent/log", data={"text": "I had two rice cakes", "tz_offset": "0"})
    assert r.status_code == 200
    a = r.json()["annotation"]
    assert a["meal"] in ("breakfast", "lunch", "dinner", "snack")
    assert a["meal_label"] == "rice cake"
    assert a["specificity"] == "medium"     # count phrasing parses at 0.5 confidence

    with get_conn() as conn:
        row = conn.execute("SELECT meal, specificity FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert row["meal"] == a["meal"]


def test_fast_path_annotation_high_specificity_for_grams():
    from app.services.agent import fast_path_annotation
    a = fast_path_annotation("100g chicken breast", [{"food_name": "Chicken Breast"}], tz_offset=0)
    assert a["specificity"] == "high"
    assert a["meal_label"] == "chicken breast"


def test_local_hour_meal_buckets(monkeypatch):
    from app.services import agent as agent_svc
    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc)   # 13:00 UTC

    monkeypatch.setattr(agent_svc, "datetime", _FakeDT)
    assert agent_svc.local_hour_meal(0) == "lunch"          # 13:00 local
    assert agent_svc.local_hour_meal(300) == "breakfast"    # 08:00 local (UTC-5)
    assert agent_svc.local_hour_meal(-600) == "snack"       # 23:00 local (UTC+10)


# ── Follow-up refinement ("say more" / "add photo" after logging) ─────────────

def _first_capture_id(uid):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id FROM capture_log WHERE user_id=? ORDER BY id LIMIT 1", (uid,)
        ).fetchone()["id"]


def test_revision_updates_and_adds_entries(client, monkeypatch):
    """Voice log first; a follow-up capture fixes the quantity and adds an item."""
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()                                   # food 1: rice cake
    _seed_food(name="blackberries", serving_g=None)  # food 2

    # Initial log via fast path (user knows rice cakes).
    _log_once(uid, 1)
    r1 = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    cap_id = r1.json()["capture_id"]
    entry_id = r1.json()["entries"][0]["id"]
    assert cap_id

    # Follow-up: agent revises quantity and logs the berries it now sees.
    _script_llm(monkeypatch, [
        _tool("update_entry", {"entry_id": entry_id, "quantity_g": 45}, "t1"),
        _tool("log_food", {"food_id": 2, "quantity_g": 100}, "t2"),
        _text("Updated the rice cakes and added the blackberries."),
    ])
    r2 = client.post("/api/agent/log",
                     data={"text": "actually it was five cakes with blackberries",
                           "revise_capture_id": str(cap_id)})
    assert r2.status_code == 200
    d = r2.json()
    assert d["revised"] is True
    by_name = {e["food_name"]: e for e in d["entries"]}
    assert by_name["rice cake"]["quantity_g"] == pytest.approx(45.0)
    assert "blackberries" in by_name

    # The follow-up capture row links to the original.
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM capture_log WHERE id=?", (d["capture_id"],)).fetchone()
    assert row["parent_capture_id"] == cap_id
    entries = json.loads(row["entries_json"])
    assert {e["food_name"] for e in entries} == {"rice cake", "blackberries"}

    # And the live log entry really changed.
    with get_conn() as conn:
        q = conn.execute("SELECT quantity_g FROM log_entries WHERE id=?", (entry_id,)).fetchone()
    assert q["quantity_g"] == pytest.approx(45.0)


def test_revision_can_remove_entries(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()
    _log_once(uid, 1)
    r1 = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    cap_id = r1.json()["capture_id"]
    entry_id = r1.json()["entries"][0]["id"]

    _script_llm(monkeypatch, [
        _tool("remove_entry", {"entry_id": entry_id}, "t1"),
        _text("Removed it — that wasn't eaten after all."),
    ])
    r2 = client.post("/api/agent/log",
                     data={"text": "scratch that, I didn't eat them",
                           "revise_capture_id": str(cap_id)})
    assert r2.status_code == 200
    assert r2.json()["entries"] == []
    with get_conn() as conn:
        assert conn.execute("SELECT id FROM log_entries WHERE id=?", (entry_id,)).fetchone() is None


def test_revision_rejects_foreign_capture(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    with get_conn() as conn:
        conn.execute("INSERT INTO users (email, password_hash, display_name) VALUES ('o@o.com','x','O')")
        conn.execute(
            "INSERT INTO capture_log (user_id, input_type, transcript, entries_json) VALUES (?, 'voice', 'x', '[]')",
            (uid + 1,))
        foreign = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    r = client.post("/api/agent/log", data={"text": "more", "revise_capture_id": str(foreign)})
    assert r.status_code == 404


def test_update_entry_ownership(client):
    uid = _register(client)
    _seed_food()
    e = _log_once(uid, 1)
    from app.services.logging import update_entry_quantity, FoodNotFound
    updated = update_entry_quantity(uid, e["id"], 50)
    assert updated["quantity_g"] == 50
    with pytest.raises(FoodNotFound):
        update_entry_quantity(uid + 1, e["id"], 60)   # someone else's entry


# ── Photo persistence (dataset material) ──────────────────────────────────────

def test_photo_capture_saved_to_disk(client, scripted_agent, monkeypatch, tmp_path):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    uid = _register(client)
    _seed_food()
    scripted_agent()

    r = client.post("/api/agent/log", files={"image": ("meal.png", io.BytesIO(_PNG), "image/png")})
    assert r.status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT photo_path FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert row["photo_path"] and row["photo_path"].endswith(".png")
    import os
    assert os.path.exists(row["photo_path"])
    with open(row["photo_path"], "rb") as f:
        assert f.read() == _PNG


def test_account_delete_removes_photo_files(client, scripted_agent, monkeypatch, tmp_path):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    uid = _register(client)
    _seed_food()
    scripted_agent()
    client.post("/api/agent/log", files={"image": ("meal.png", io.BytesIO(_PNG), "image/png")})
    with get_conn() as conn:
        path = conn.execute("SELECT photo_path FROM capture_log WHERE user_id=?", (uid,)).fetchone()["photo_path"]
    import os
    assert os.path.exists(path)

    client.request("DELETE", "/api/auth/account", json={"password": REG["password"]})
    assert not os.path.exists(path)


# ── Dataset export ────────────────────────────────────────────────────────────

def test_export_dataset_merges_chain_and_marks_undone(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()
    _log_once(uid, 1)
    r1 = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    cap_id, entry_id = r1.json()["capture_id"], r1.json()["entries"][0]["id"]

    _script_llm(monkeypatch, [
        _tool("update_entry", {"entry_id": entry_id, "quantity_g": 45}, "t1"),
        _text("Fixed."),
    ])
    client.post("/api/agent/log", data={"text": "make it five", "revise_capture_id": str(cap_id)})

    # The user later undoes the entry entirely → kept=false in the dataset.
    client.delete(f"/api/log/{entry_id}")

    import io as _io
    from scripts.export_dataset import export
    buf = _io.StringIO()
    export(buf)
    lines = [json.loads(l) for l in buf.getvalue().splitlines()]
    # only root captures export (the revision is folded into its parent)
    roots = [l for l in lines if l["capture_id"] == cap_id]
    assert len(roots) == 1
    ex = roots[0]
    assert len(ex["inputs"]) == 2                      # original + follow-up
    assert ex["inputs"][1]["text"] == "make it five"
    assert ex["items"][0]["kept"] is False             # undone after export snapshot


def test_revision_blocks_duplicate_relog(client, monkeypatch):
    """In revision mode, log_food on an already-logged food is rejected with a
    pointer to update_entry — the model self-corrects instead of duplicating."""
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()
    _log_once(uid, 1)
    r1 = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    cap_id = r1.json()["capture_id"]
    entry_id = r1.json()["entries"][0]["id"]

    # The scripted model tries to re-log food 1 (the duplicate), gets the guard
    # error back, then correctly updates instead.
    _script_llm(monkeypatch, [
        _tool("log_food", {"food_id": 1, "servings": 2}, "t1"),
        _tool("update_entry", {"entry_id": entry_id, "quantity_g": 27}, "t2"),
        _text("Confirmed from the photo — adjusted to three cakes."),
    ])
    r2 = client.post("/api/agent/log",
                     files={"image": ("meal.png", io.BytesIO(_PNG), "image/png")},
                     data={"revise_capture_id": str(cap_id)})
    assert r2.status_code == 200
    d = r2.json()
    assert len(d["entries"]) == 1                      # no duplicate
    assert d["entries"][0]["quantity_g"] == pytest.approx(27.0)
    # photo follow-up keeps the original words visible
    assert d["transcript"] == "I had two rice cakes"


def test_entries_carry_serving_info_for_equivalents(client):
    """Result-card and log-pane entries include serving_g/serving_desc so the
    UI can show human-scale equivalents ("18g ≈ 2 cakes") next to grams."""
    uid = _register(client)
    food_id = _seed_food()                      # '1 cake', serving_g 9.0
    _log_once(uid, food_id)

    r = client.post("/api/agent/log", data={"text": "I had two rice cakes"})
    e = r.json()["entries"][0]
    assert e["serving_g"] == pytest.approx(9.0)
    assert e["serving_desc"] == "1 cake"

    day = client.get("/api/log/today").json()
    assert day[0]["serving_g"] == pytest.approx(9.0)
    assert day[0]["serving_desc"] == "1 cake"


# ── Audio persistence (dataset material + STT-failure replay) ─────────────────

def _fake_transcribe(text):
    async def f(blob):
        return text
    return f


def test_voice_capture_audio_saved_to_disk(client, monkeypatch, tmp_path):
    import os
    from app.routers import agent as agent_router
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(agent_router.stt, "transcribe", _fake_transcribe("I had two rice cakes"))
    uid = _register(client)
    _log_once(uid, _seed_food())

    r = client.post("/api/agent/log",
                    files={"audio": ("a.webm", io.BytesIO(b"opusdata"), "audio/webm")})
    assert r.status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT audio_path FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert row["audio_path"] and row["audio_path"].endswith(".webm")
    assert os.path.exists(row["audio_path"])
    with open(row["audio_path"], "rb") as f:
        assert f.read() == b"opusdata"


def test_no_speech_records_failed_capture_with_audio(client, monkeypatch, tmp_path):
    """A silent recording (the Whisper-hallucination case) 422s to the user but
    keeps the audio + a capture row so the failure can be replayed later."""
    import os
    from app.routers import agent as agent_router
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(agent_router.stt, "transcribe", _fake_transcribe(""))
    uid = _register(client)

    r = client.post("/api/agent/log",
                    files={"audio": ("a.mp4", io.BytesIO(b"nearsilence"), "audio/mp4")})
    assert r.status_code == 422
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM capture_log WHERE user_id=?", (uid,)).fetchone()
    assert row["summary"] == "(no speech detected)"
    assert row["transcript"] is None
    assert json.loads(row["entries_json"]) == []
    assert row["audio_path"].endswith(".m4a")
    assert os.path.exists(row["audio_path"])


def test_account_delete_removes_audio_files(client, monkeypatch, tmp_path):
    import os
    from app.routers import agent as agent_router
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(agent_router.stt, "transcribe", _fake_transcribe("I had two rice cakes"))
    uid = _register(client)
    _log_once(uid, _seed_food())
    client.post("/api/agent/log", files={"audio": ("a.webm", io.BytesIO(b"x"), "audio/webm")})
    with get_conn() as conn:
        path = conn.execute("SELECT audio_path FROM capture_log WHERE user_id=?",
                            (uid,)).fetchone()["audio_path"]
    assert os.path.exists(path)

    client.request("DELETE", "/api/auth/account", json={"password": REG["password"]})
    assert not os.path.exists(path)


def test_export_dataset_includes_audio_and_issue_reports(client, monkeypatch, tmp_path):
    """A 'model'-category issue filed against a capture rides along in the
    export — the user's complaint is a label for that example."""
    from app.routers import agent as agent_router
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(agent_router.stt, "transcribe", _fake_transcribe("I had two rice cakes"))
    uid = _register(client)
    _log_once(uid, _seed_food())
    r1 = client.post("/api/agent/log", files={"audio": ("a.webm", io.BytesIO(b"x"), "audio/webm")})
    cap_id = r1.json()["capture_id"]

    async def fake_triage(message, context=None, user_id=None):
        return "model"
    monkeypatch.setattr("app.services.triage.classify_issue", fake_triage)
    client.post("/api/issues/", json={"message": "wrong portion", "capture_id": cap_id})

    import io as _io
    from scripts.export_dataset import export
    buf = _io.StringIO()
    export(buf)
    ex = next(json.loads(l) for l in buf.getvalue().splitlines()
              if json.loads(l)["capture_id"] == cap_id)
    assert ex["inputs"][0]["audio"].endswith(".webm")
    assert ex["issues"] == [{"category": "model", "message": "wrong portion"}]

    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM log_entries WHERE user_id=?", (uid,)).fetchone()["c"]
    assert n == 2   # the _log_once seed + the capture's entry — no third (duplicate) row
