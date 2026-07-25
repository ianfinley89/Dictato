"""Passive weigh-in capture. A FALSE positive silently corrupts the weight trend
that bias-correction would be calibrated against, so the negative cases matter
at least as much as the positive ones."""
import pytest

from app.services.weight import (
    parse_weight, is_weight_only, record_weight, latest_weight, recent_weights,
    record_from_facts, LB_PER_KG,
)

REG = {"email": "weight@example.com", "password": "password123", "display_name": "W"}


def _register(client) -> int:
    r = client.post("/api/auth/register", json=REG)
    assert r.status_code == 200
    return r.json()["user_id"]


# ── Parsing: the phrasings people actually use ───────────────────────────────
@pytest.mark.parametrize("text,lb", [
    ("I weighed 182 this morning", 182),
    ("weighed in at 182.4 lbs today", 182.4),
    ("I'm 182 pounds", 182),
    ("the scale said 175 this morning", 175),
    ("stepped on the scale, 190", 190),
    ("I weigh 182 lb", 182),
    ("I'm down to 178 lbs", 178),
])
def test_parses_real_weigh_ins(text, lb):
    w = parse_weight(text)
    assert w is not None, text
    assert w["weight_kg"] == pytest.approx(lb / LB_PER_KG, abs=0.02)


def test_parses_metric():
    w = parse_weight("I weighed 84.2 kg this morning")
    assert w["weight_kg"] == pytest.approx(84.2)
    assert w["unit"] == "kg"


# ── The dangerous cases: things that must NOT become weigh-ins ──────────────
@pytest.mark.parametrize("text", [
    "I ate 182 grams of chicken",
    "two rice cakes and a coffee",
    "I want to weigh 180 by summer",          # a goal, not a measurement
    "my goal weight is 175",
    "I'm trying to get to 170 pounds",
    "I'm 30 minutes late",                    # bare number, no unit
    "I had 12 wings",
    "",
])
def test_rejects_non_weigh_ins(text):
    assert parse_weight(text) is None, text


def test_rejects_implausible_values():
    assert parse_weight("I weighed 12 pounds") is None      # below the floor
    assert parse_weight("I weighed 900 pounds") is None


# ── Unit inference uses the user's own history ──────────────────────────────
def test_bare_number_prefers_history():
    # 84 with a 84kg history is kg; the same words with a 180lb history are lb.
    assert parse_weight("I weighed 84", last_kg=84.0)["unit"] == "kg"
    assert parse_weight("I weighed 84", last_kg=81.6)["unit"] == "kg"
    w = parse_weight("I weighed 182", last_kg=82.5)
    assert w["unit"] == "lb" and w["weight_kg"] == pytest.approx(82.6, abs=0.2)


# ── "Was that ONLY a weigh-in?" gates the zero-token path ───────────────────
def test_weight_only_detection():
    t = "I weighed 182 this morning"
    assert is_weight_only(t, parse_weight(t)["span"])
    t2 = "I weighed 182 and had two eggs"
    assert not is_weight_only(t2, parse_weight(t2)["span"])


# ── Storage ─────────────────────────────────────────────────────────────────
def test_record_and_read_back(client):
    uid = _register(client)
    record_weight(uid, 82.5, "lb")
    got = latest_weight(uid)
    assert got["weight_kg"] == pytest.approx(82.5) and got["unit"] == "lb"
    assert len(recent_weights(uid)) == 1


def test_same_day_same_source_corrects_not_duplicates(client):
    uid = _register(client)
    record_weight(uid, 82.5, "lb", source="voice")
    record_weight(uid, 83.0, "lb", source="voice")
    rows = recent_weights(uid)
    assert len(rows) == 1 and rows[0]["weight_kg"] == pytest.approx(83.0)


def test_coach_facts_are_mirrored_into_the_table(client):
    """The profile is the model's scratchpad; weigh_ins is the source of truth."""
    uid = _register(client)
    assert record_from_facts(uid, {"weight_lb": 182}) == 1
    assert latest_weight(uid)["weight_kg"] == pytest.approx(182 / LB_PER_KG, abs=0.02)
    assert record_from_facts(uid, {"favorite_food": "tacos"}) == 0


def test_profile_update_mirrors_weight(client):
    from app.services.profile import apply_profile_update
    uid = _register(client)
    apply_profile_update(uid, {"weight_lb": 180, "rides_bike": True})
    assert latest_weight(uid) is not None


# ── End to end through the capture endpoint (zero model tokens) ─────────────
def test_voice_weigh_in_logs_no_food_and_costs_nothing(client, monkeypatch):
    uid = _register(client)

    async def _boom(*a, **k):                      # the model must NOT be called
        raise AssertionError("agent ran for a weight-only capture")
    monkeypatch.setattr("app.services.agent.run_agent", _boom)

    r = client.post("/api/agent/log", data={"text": "I weighed 182 this morning",
                                            "tz_offset": "0"})
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == [] and body["fast_path"] is True
    assert "182" in body["summary"]
    assert latest_weight(uid)["weight_kg"] == pytest.approx(182 / LB_PER_KG, abs=0.02)


def test_settings_weight_endpoints(client):
    _register(client)
    r = client.post("/api/auth/weight", json={"value": 182, "unit": "lb"})
    assert r.status_code == 200
    wid = r.json()["entry"]["id"]
    assert client.get("/api/auth/weight").json()["latest"]["id"] == wid
    assert client.delete(f"/api/auth/weight/{wid}").status_code == 200
    assert client.get("/api/auth/weight").json()["latest"] is None


def test_weight_endpoint_rejects_out_of_range(client):
    _register(client)
    assert client.post("/api/auth/weight", json={"value": 5, "unit": "kg"}).status_code == 400
