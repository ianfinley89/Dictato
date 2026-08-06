"""Flagging captures that a recipe would have served better.

A homemade dish can share a name with a standard one and share nothing else.
`strong_db_match` cannot see that — it found "Chickpea Curry" for the user whose
curry used blended tomatoes instead of coconut milk, which is precisely why the
agent grounded it there. The only evidence is what the user SAID, so that is
what this reads.

Deliberately no model: asking one "is this a recipe?" would be a self-report,
and self-reports have already measured worthless here (portion confidence,
AUROC 0.556).
"""
from app.services.recipe_candidate import describes_composition, is_candidate, tag_for


# ── The real capture this exists for ─────────────────────────────────────────
CURRY = "This is chickpea curry but instead of coconut milk it's made with blended vegetables like tomatoes, garlic, onion, and olive oil."
SOUP = "I had homemade chicken orzo soup and another round of broccoli"


def test_the_capture_that_prompted_this_is_flagged():
    assert describes_composition(CURRY)
    # It logged the curry AND a pork tenderloin, so any entry-count test misses
    # it — an earlier version did exactly that.
    assert is_candidate(CURRY, [{"food_name": "Chickpea Curry"},
                                {"food_name": "Pork, Tenderloin"}])


def test_homemade_is_enough():
    assert is_candidate(SOUP, [{"food_name": "Chicken Orzo Soup"}])


# ── What must NOT fire: a meal is not a recipe ───────────────────────────────
def test_listing_several_foods_is_not_composition():
    """"with X and Y" was tried as a signal and tripled the fire rate with
    noise. A biscuit, gravy and a sausage link are three foods, not a recipe."""
    for meal in ("One biscuit with gravy and a sausage link",
                 "For breakfast I had cereal with milk and blackberries",
                 "I had a fiber protein bar and I had coffee with cream"):
        assert not describes_composition(meal), meal


def test_plain_logs_are_untouched():
    for t in ("I had two rice cakes", "a bowl of chickpea curry",
              "large iced coffee", ""):
        assert not is_candidate(t, [{"food_name": "x"}]), t


# ── The invisible case: a correction that landed nothing ─────────────────────
def test_a_correction_that_did_not_land_is_always_a_candidate():
    """It reads as a clean capture in every other signal precisely because the
    entries did not move — this is the only thing that can see it."""
    assert is_candidate(CURRY, [{"food_name": "Chickpea Curry"}],
                        ["correction:none-applied"])


def test_tag_for_returns_the_taggable_list():
    assert tag_for(CURRY, [{"food_name": "Chickpea Curry"}]) == ["recipe-candidate"]
    assert tag_for("I had two rice cakes", [{"food_name": "Rice Cake"}]) == []
