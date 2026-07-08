"""Coach chat: history, the mocked chat loop, and hands-off profile building.
No real Anthropic calls."""
import json
import types

import pytest

from app.database import get_conn

REG = {"email": "coach@example.com", "password": "password123", "display_name": "C"}


def _register(client):
    client.post("/api/auth/register", json=REG)
    with get_conn() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?", (REG["email"],)).fetchone()["id"]


# ── Endpoint guards ───────────────────────────────────────────────────────────

def test_coach_history_requires_auth(client):
    assert client.get("/api/coach/history").status_code == 401


def test_coach_history_empty(client):
    _register(client)
    r = client.get("/api/coach/history")
    assert r.status_code == 200
    assert r.json() == {"messages": [], "profile": {}}


def test_coach_chat_rejects_empty(client):
    _register(client)
    r = client.post("/api/coach/chat", json={"message": "   "})
    assert r.status_code == 400


def test_coach_chat_503_without_key(client, monkeypatch):
    from app.routers import coach as coach_router
    monkeypatch.setattr(coach_router, "ANTHROPIC_API_KEY", "")
    _register(client)
    r = client.post("/api/coach/chat", json={"message": "hi"})
    assert r.status_code == 503


def test_coach_chat_rate_limited(client, monkeypatch):
    from app.routers import coach as coach_router
    monkeypatch.setattr(coach_router, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_LIMIT", "0")
    _register(client)
    r = client.post("/api/coach/chat", json={"message": "hi"})
    assert r.status_code == 429


# ── Mocked chat loop ──────────────────────────────────────────────────────────

def _block(**kw):
    return types.SimpleNamespace(**kw)


def _resp(blocks, stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=blocks, stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=20, output_tokens=10),
    )


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _install(monkeypatch, responses):
    from app.services import coach as coach_svc
    from app.routers import coach as coach_router
    monkeypatch.setattr(coach_router, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(coach_svc, "_client", lambda: _FakeClient(responses))


def test_coach_chat_replies_and_persists(client, monkeypatch):
    _install(monkeypatch, [_resp([_block(type="text", text="You're tracking well — keep it up!")])])
    _register(client)

    r = client.post("/api/coach/chat", json={"message": "How am I doing?"})
    assert r.status_code == 200
    assert r.json()["reply"] == "You're tracking well — keep it up!"

    # both turns persisted, in order
    hist = client.get("/api/coach/history").json()["messages"]
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "How am I doing?"


def test_coach_remembers_profile_facts(client, monkeypatch):
    """A weigh-in mentioned in passing is saved via the update_profile tool."""
    responses = [
        _resp([_block(type="tool_use", id="t1", name="update_profile",
                      input={"facts": {"sex": "male", "weigh_ins": [{"date": "2026-07-08", "lbs": 182}]}})],
              stop_reason="tool_use"),
        _resp([_block(type="text", text="Got it, logged you at 182. Nice work this week.")]),
    ]
    _install(monkeypatch, responses)
    _register(client)

    r = client.post("/api/coach/chat", json={"message": "morning weigh-in was 182, I'm a guy trying to bulk"})
    assert r.status_code == 200
    profile = r.json()["profile"]
    assert profile["sex"] == "male"
    assert profile["weigh_ins"][0]["lbs"] == 182


def test_coach_merges_weigh_ins_across_turns(client, monkeypatch):
    from app.services import coach as coach_svc
    uid = _register(client)
    coach_svc._apply_profile_update(uid, {"weigh_ins": [{"date": "2026-07-01", "lbs": 180}]})
    coach_svc._apply_profile_update(uid, {"weigh_ins": [{"date": "2026-07-08", "lbs": 182}]})
    profile = coach_svc.get_profile(uid)
    assert len(profile["weigh_ins"]) == 2
    assert profile["weigh_ins"][-1]["lbs"] == 182


def test_merge_facts_helper():
    from app.services.coach import _merge_facts
    old = {"sex": "male", "dislikes": ["olives"], "goal": "cut"}
    new = {"goal": "bulk", "dislikes": ["mushrooms"], "height_cm": 178}
    merged = _merge_facts(old, new)
    assert merged["goal"] == "bulk"                     # scalar overwritten
    assert merged["dislikes"] == ["olives", "mushrooms"]  # lists concatenated
    assert merged["height_cm"] == 178                    # new key added
    assert merged["sex"] == "male"                       # untouched key kept
