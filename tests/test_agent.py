"""Tests for the agentic logging endpoint: fast path, tool execution, and the
mocked model loop. No real Anthropic or Whisper calls."""
import io
import json
import types

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


def test_tool_create_food_web_is_public_and_clamped(client):
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
    assert food["nutrients_per_100g"]["calories"] == pytest.approx(900.0)  # clamped
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


# ── Mocked model loop ─────────────────────────────────────────────────────────

def _block(**kw):
    return types.SimpleNamespace(**kw)


def _resp(blocks, stop_reason):
    return types.SimpleNamespace(
        content=blocks, stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


@pytest.fixture()
def scripted_agent(monkeypatch):
    """Patch the agent's model client with a scripted search→log→summary run."""
    from app.services import agent as agent_svc

    def install(food_name="rice cake"):
        responses = [
            _resp([_block(type="tool_use", id="t1", name="search_food_db",
                          input={"query": food_name})], "tool_use"),
            # The fake model logs food_id 1 — tests seed exactly one food.
            _resp([_block(type="tool_use", id="t2", name="log_food",
                          input={"food_id": 1, "servings": 2})], "tool_use"),
            _resp([_block(type="text", text=f"Logged 2 {food_name}s for you!")], "end_turn"),
        ]
        monkeypatch.setattr(agent_svc, "_client", lambda: _FakeClient(responses))

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
    from app.services import agent as agent_svc
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    uid = _register(client)
    _seed_food()

    class _DyingMessages:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp([_block(type="tool_use", id="t1", name="log_food",
                                     input={"food_id": 1, "quantity_g": 50})], "tool_use")
            raise RuntimeError("api down")

    fake = types.SimpleNamespace(messages=_DyingMessages())
    monkeypatch.setattr(agent_svc, "_client", lambda: fake)

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
