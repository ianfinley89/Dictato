"""Coach chat: history, the mocked chat loop, and hands-off profile building.
No real model calls."""
import json

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

from app.services.llm import LLMResponse, ToolCall


def _text(t):
    return LLMResponse(text=t, tool_calls=[], stop_reason="end",
                       input_tokens=20, output_tokens=10, raw={"role": "assistant", "content": t})


def _tool(name, inp, tid="t1"):
    return LLMResponse(text="", tool_calls=[ToolCall(tid, name, inp)], stop_reason="tool_use",
                       input_tokens=20, output_tokens=10, raw=None)


def _install(monkeypatch, responses):
    from app.services import llm
    from app.routers import coach as coach_router
    monkeypatch.setattr(coach_router, "ANTHROPIC_API_KEY", "test-key")
    seq = list(responses)

    async def fake_chat(**kwargs):
        return seq.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)


def test_coach_chat_replies_and_persists(client, monkeypatch):
    _install(monkeypatch, [_text("You're tracking well — keep it up!")])
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
    _install(monkeypatch, [
        _tool("update_profile", {"facts": {"sex": "male", "weigh_ins": [{"date": "2026-07-08", "lbs": 182}]}}),
        _text("Got it, logged you at 182. Nice work this week."),
    ])
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


# ── calc_targets: deterministic target math ───────────────────────────────────

def test_calc_targets_male_metric():
    from app.services.coach import calc_targets
    out = calc_targets({"sex": "male", "age": 30, "height_cm": 180, "weight_kg": 80,
                        "activity_level": "moderate", "goal": "bulk"})
    # BMR = 10*80 + 6.25*180 - 5*30 + 5 = 1780; TDEE = 1780*1.55 = 2759; bulk +300
    assert out["bmr"] == 1780
    assert out["tdee"] == 2759
    assert out["suggested_calories"] == 3059
    assert out["suggested_protein_g"] == 144


def test_calc_targets_imperial_conversion():
    from app.services.coach import calc_targets
    # The user's 6'9" / 150 lbs corner case: tall + light = high TDEE despite low weight.
    out = calc_targets({"sex": "male", "age": 25, "height_in": 81, "weight_lbs": 150,
                        "activity_level": "light", "goal": "bulk"})
    assert out["bmr"] == round(10 * 150 * 0.4536 + 6.25 * 81 * 2.54 - 5 * 25 + 5)
    assert out["suggested_calories"] > 2400   # confirms "raise to 2400+" is what the math says


def test_calc_targets_requires_stats():
    from app.services.coach import calc_targets
    assert "error" in calc_targets({"sex": "male", "age": 30, "activity_level": "light", "goal": "cut"})
    assert "error" in calc_targets({"sex": "male", "age": 300, "height_cm": 180,
                                    "weight_kg": 80, "activity_level": "light", "goal": "cut"})


def test_coach_uses_calc_targets_tool(client, monkeypatch):
    """The coach calls calc_targets, gets deterministic numbers back, and replies."""
    _install(monkeypatch, [
        _tool("calc_targets", {"sex": "male", "age": 25, "height_in": 81,
                               "weight_lbs": 150, "activity_level": "light", "goal": "bulk"}),
        _text("Based on your stats you'd maintain around 2,600 — for a bulk aim near 2,900. You can set that in Settings → Set goals."),
    ])
    _register(client)
    r = client.post("/api/coach/chat", json={"message": "I'm 6'9 and 150lbs, 25yo male, lightly active — how much should I eat to bulk?"})
    assert r.status_code == 200
    assert "Settings" in r.json()["reply"]


def test_coach_context_includes_annotations(client):
    from app.services.coach import _build_context
    from app.database import get_conn
    uid = _register(client)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO capture_log (user_id, input_type, transcript, entries_json,
                                        meal, meal_label, tags_json, specificity)
               VALUES (?,?,?,?,?,?,?,?)""",
            (uid, "voice", "taco bell after biking", "[]",
             "lunch", "taco bell run", '["activity:cycling"]', "low"),
        )
    ctx = _build_context(uid, {})
    assert "activity:cycling" in ctx
    assert "taco bell run" in ctx
    assert "low" in ctx
