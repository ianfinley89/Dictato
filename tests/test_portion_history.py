"""Personal portion priors: what THIS user actually eats beats a generic guess.

The gates matter more than the feature. Live data showed portions are habitual
(median CV 0.12) EXCEPT where the same food plays different roles — milk came in
at CV 0.85 (a splash in coffee vs. a glass). A prior there would be confidently
wrong, so it must not fire at all.
"""
import pytest

from app.database import get_conn
from app.services.portion import apply_personal_prior
from app.services.portion_history import personal_prior

REG = {"email": "prior@example.com", "password": "password123", "display_name": "P"}


def _register(client) -> int:
    r = client.post("/api/auth/register", json=REG)
    assert r.status_code == 200
    return r.json()["user_id"]


def _food(name="rice cake", serving_g=9.0) -> int:
    import json
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, name, serving_g, nutrients_json)
               VALUES ('usda', ?, ?, ?)""",
            (name, serving_g, json.dumps({"calories": 387.0, "protein_g": 8.0,
                                          "carbs_g": 82.0, "fat_g": 3.0})))
        return cur.lastrowid


def _log(uid: int, food_id: int, grams: float, source: str = "voice") -> None:
    import json
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO log_entries (user_id, food_id, eaten_at, quantity_g,
                                        nutrients_snapshot_json, source)
               VALUES (?,?,datetime('now'),?,?,?)""",
            (uid, food_id, grams, json.dumps({"calories": 10.0}), source))


# ── Gating ───────────────────────────────────────────────────────────────────
def test_no_prior_without_enough_history(client):
    uid, fid = _register(client), _food()
    assert personal_prior(uid, fid) is None
    _log(uid, fid, 11.0)
    _log(uid, fid, 11.0)
    assert personal_prior(uid, fid) is None          # 2 kept logs is not a habit


def test_habitual_food_yields_a_prior(client):
    uid, fid = _register(client), _food()
    for g in (11.0, 12.0, 11.0):
        _log(uid, fid, g)
    p = personal_prior(uid, fid)
    assert p["grams"] == pytest.approx(11.0) and p["n"] == 3
    assert p["kind"] == "habitual" and p["cv"] < 0.25


def test_context_dependent_food_gets_no_prior(client):
    """The milk case: a splash in coffee and a full glass are the same food.
    High variance must yield NO prior rather than a confident wrong one."""
    uid, fid = _register(client), _food(name="milk", serving_g=240.0)
    for g in (150.0, 30.0, 48.0):                     # the real observed values
        _log(uid, fid, g)
    assert personal_prior(uid, fid) is None


def test_one_adjust_teaches_the_portion_forever(client):
    """The headline behaviour: the user corrects a portion ONCE (Adjust posts
    source='manual') and the app stops guessing that food. A single stated
    number outranks any pile of accepted guesses."""
    uid, fid = _register(client), _food()
    _log(uid, fid, 100.0, source="photo")            # the model's bad guess, accepted
    assert personal_prior(uid, fid) is None          # one accepted log is not a habit
    _log(uid, fid, 11.0, source="manual")            # user fixes it via Adjust
    p = personal_prior(uid, fid)
    assert p["kind"] == "verified" and p["n"] == 1
    assert p["grams"] == pytest.approx(11.0)


def test_hand_corrected_portions_outrank_accepted_ones(client):
    """source='manual' means the user typed it via Adjust — verified, so two are
    enough, and they win over a noisier pile of merely-accepted logs."""
    uid, fid = _register(client), _food()
    for g in (100.0, 5.0, 250.0):                     # accepted junk, high CV
        _log(uid, fid, g, source="voice")
    _log(uid, fid, 11.0, source="manual")
    _log(uid, fid, 11.0, source="manual")
    p = personal_prior(uid, fid)
    assert p["kind"] == "verified" and p["grams"] == pytest.approx(11.0)


def test_prior_is_scoped_to_the_user(client):
    uid, fid = _register(client), _food()
    other = client.post("/api/auth/register", json={
        "email": "other@example.com", "password": "password123",
        "display_name": "O"}).json()["user_id"]
    for g in (11.0, 11.0, 11.0):
        _log(other, fid, g)                           # someone else's habit
    assert personal_prior(uid, fid) is None           # hard rule #6: scoped by user


# ── Substitution rules ───────────────────────────────────────────────────────
def test_prior_replaces_a_blind_estimate():
    res = {"grams": 100.0, "basis": "estimate", "confidence": "low", "note": None}
    out = apply_personal_prior(res, {"grams": 11.0, "n": 4, "cv": 0.05, "kind": "habitual"})
    assert out["grams"] == 11.0
    assert out["basis"] == "history" and out["confidence"] == "medium"
    assert "usual portion" in out["note"]


@pytest.mark.parametrize("basis", ["stated", "label", "count", "household"])
def test_prior_never_overrides_evidence_about_this_meal(basis):
    res = {"grams": 300.0, "basis": basis, "confidence": "high", "note": None}
    out = apply_personal_prior(res, {"grams": 11.0, "n": 9, "cv": 0.01, "kind": "verified"})
    assert out == res


def test_no_prior_is_a_noop():
    res = {"grams": 100.0, "basis": "estimate", "confidence": "low", "note": None}
    assert apply_personal_prior(res, None) == res


# ── End to end through the logging tool ──────────────────────────────────────
def test_log_food_uses_the_users_habit_instead_of_guessing(client):
    uid = _register(client)
    fid = _food()
    for g in (11.0, 11.0, 12.0):
        _log(uid, fid, g)
    import asyncio
    from app.services.agent import _tool_log_food
    logged = []
    out = asyncio.run(_tool_log_food(
        uid, {"food_id": fid, "quantity_g": 100, "basis": "estimate"},
        "voice", "a rice cake", logged))
    assert out["quantity_g"] == pytest.approx(11.0)   # not the model's 100g
    assert out["portion_basis"] == "history"
    assert logged[0]["portion_model_g"] == 100.0      # what the model would have said
