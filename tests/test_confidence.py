"""Deciding when to ask the user for more detail.

The score must come from verified facts only — asking the model to rate itself
measured AUROC 0.556 on real ground truth. And it must fire RARELY: a strip that
appears on every capture is wallpaper, which is the failure mode this replaced.
"""
import pytest

from app.services.confidence import (score_capture, needs_clarification,
                                     MIN_STAKE_CALORIES)

BIG = 400.0            # above MIN_STAKE_CALORIES, so stakes are real


def _entry(basis="count", cal=BIG, serving_g=100.0, snapped=False, source="usda"):
    return {"portion_basis": basis, "calories": cal, "serving_g": serving_g,
            "portion_snapped": snapped, "food_source_raw": source}


# ── Failed captures: not a judgement call, a fact ────────────────────────────
def test_nothing_logged_scores_zero():
    s = score_capture([], "I couldn't identify anything to log.")
    assert s["score"] == 0.0 and s["reason"] == "nothing-logged"


def test_model_asking_a_question_scores_zero():
    """The Red curry case: the agent asked "was it chicken or tofu?" and logged
    nothing, so the user got an empty card."""
    s = score_capture([_entry()], "Was the red curry made with chicken or tofu?")
    assert s["score"] == 0.0 and s["reason"] == "model-asked-a-question"
    assert s["asked_question"] is True


# ── Grounded captures must never nag ────────────────────────────────────────
@pytest.mark.parametrize("basis", ["stated", "label", "count"])
def test_evidence_backed_portions_are_fully_trusted(basis):
    s = score_capture([_entry(basis=basis)], "Logged it.")
    assert s["score"] == 1.0
    assert needs_clarification([_entry(basis=basis)], "Logged it.", 0.5)["clarify"] is False


def test_household_and_history_are_good_enough():
    for basis in ("household", "history"):
        assert not needs_clarification([_entry(basis=basis)], "ok", 0.5)["clarify"]


# ── Guesses: bounded is tolerable, unanchored is not ────────────────────────
def test_unanchored_guess_asks_for_help():
    """No serving size, no household measure — the grams had nothing to bound
    them, and this is where portions go badly wrong."""
    s = needs_clarification([_entry(basis="estimate", serving_g=None)], "ok", 0.5)
    assert s["clarify"] is True and s["score"] == 0.0


def test_bounded_guess_does_not_ask():
    """The food knows its serving size, so the guess is in the neighbourhood."""
    s = needs_clarification([_entry(basis="estimate", serving_g=210.0)], "ok", 0.5)
    assert s["clarify"] is False


def test_invented_nutrition_asks_for_help():
    s = needs_clarification([_entry(basis="estimate", source="estimate")], "ok", 0.5)
    assert s["clarify"] is True


def test_clamped_guess_counts_against_confidence():
    s = score_capture([_entry(basis="estimate", snapped=True)], "ok")
    assert s["score"] < score_capture([_entry(basis="estimate")], "ok")["score"]


# ── Weighting: the big item decides ─────────────────────────────────────────
def test_calories_decide_whose_guess_matters():
    """A guessed garnish beside a well-grounded main should not trigger a prompt;
    a guessed main beside a known garnish should."""
    garnish_guessed = [_entry(basis="count", cal=600.0),
                       _entry(basis="estimate", serving_g=None, cal=15.0)]
    main_guessed = [_entry(basis="estimate", serving_g=None, cal=600.0),
                    _entry(basis="count", cal=15.0)]
    assert not needs_clarification(garnish_guessed, "ok", 0.5)["clarify"]
    assert needs_clarification(main_guessed, "ok", 0.5)["clarify"]


# ── Never interrupt over trivia ─────────────────────────────────────────────
def test_low_stake_captures_are_left_alone():
    tiny = [_entry(basis="estimate", serving_g=None, cal=MIN_STAKE_CALORIES - 100)]
    s = needs_clarification(tiny, "ok", 0.5)
    assert s["reason"] == "low-stake" and s["clarify"] is False


def test_threshold_is_respected():
    e = [_entry(basis="estimate", serving_g=210.0)]     # bounded guess: 0.65
    assert not needs_clarification(e, "ok", 0.5)["clarify"]
    assert needs_clarification(e, "ok", 0.7)["clarify"]


# ── End to end ──────────────────────────────────────────────────────────────
def test_endpoint_reports_confidence(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _register, _seed_food, _script_llm, _tool, _text
    _register(client)
    _seed_food()
    # Search first, exactly as the model must: that is where a valid food_id
    # comes from (favouriting it instead would trigger the zero-token fast path
    # and never reach the agent).
    _script_llm(monkeypatch, [
        _tool("search_food_db", {"query": "rice cake"}, "s1"),
        _tool("log_food", {"food_id": 1, "basis": "count", "servings": 2,
                           "quantity_g": 18}, "t1"),
        _text("Logged two rice cakes."),
    ])
    r = client.post("/api/agent/log", data={"text": "two rice cakes for breakfast"})
    assert r.status_code == 200
    c = r.json()["confidence"]
    assert c["clarify"] is False and "score" in c and "threshold" in c


def test_endpoint_flags_a_capture_that_logged_nothing(client, monkeypatch):
    from app.routers import agent as agent_router
    monkeypatch.setattr(agent_router, "ANTHROPIC_API_KEY", "test-key")
    from tests.test_agent import _register, _script_llm, _text
    _register(client)
    _script_llm(monkeypatch, [_text("Was that chicken or tofu?")])
    r = client.post("/api/agent/log", data={"text": "red curry"})
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["confidence"]["clarify"] is True
    assert body["capture_id"]        # so the card can offer Say more / Type it
