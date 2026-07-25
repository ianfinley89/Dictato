"""Portion ladder: the model reports observations, this math must be exact."""
import json

import pytest

from app.services.portion import (
    parse_usda_portions, match_household, resolve_grams, guard_grams,
    snap_estimate, stated_number, verify_claims, portion_label,
    primary_unit_grams,
)

# "Egg, Whole, Cooked, Scrambled" as USDA actually returns it — no serving size,
# which is why a two-egg omelette used to land as bare grams.
EGG_PORTIONS = [
    {"unit": "tbsp", "qty": 1, "grams": 13.7, "desc": "1 tbsp"},
    {"unit": "large", "qty": 1, "grams": 61.0, "desc": "1 large"},
    {"unit": "cup", "qty": 1, "grams": 220.0, "desc": "1 cup"},
]

RICE = {"id": 1, "name": "Rice, white, cooked", "serving_g": None,
        "portions": [{"unit": "cup", "qty": 1, "grams": 158.0, "desc": "1 cup"}]}
TACO = {"id": 2, "name": "Taco", "serving_g": 102.0, "portions": None}
PLAIN = {"id": 3, "name": "Mystery", "serving_g": None, "portions": None}


# ── Rung 1: stated / label weights ───────────────────────────────────────────
def test_stated_weight_trusted():
    r = resolve_grams(PLAIN, {"quantity_g": 150, "basis": "stated"})
    assert (r["grams"], r["basis"], r["confidence"]) == (150, "stated", "high")


def test_label_weight_trusted():
    r = resolve_grams(PLAIN, {"quantity_g": 35, "basis": "label"})
    assert (r["grams"], r["confidence"]) == (35, "high")


# ── Rung 2: count x serving_g ────────────────────────────────────────────────
def test_count_uses_serving_g():
    r = resolve_grams(TACO, {"quantity_g": 500, "basis": "count", "servings": 3,
                             "count_verified": True})
    assert r["grams"] == 3 * 102.0          # NOT the model's 500g guess
    assert r["basis"] == "count" and r["confidence"] == "high"


def test_unverified_count_keeps_math_but_loses_confidence():
    """"an order of X" -> the model invented the count. The serving math is still
    the best anchor, but calling it high-confidence was a broken promise
    (26% of such entries were >50% wrong on Menu-Match)."""
    r = resolve_grams(TACO, {"quantity_g": 500, "basis": "count", "servings": 3})
    assert r["grams"] == 3 * 102.0          # math unchanged
    assert r["basis"] == "count" and r["confidence"] == "low"
    assert "inferred" in r["note"]


def test_count_without_serving_g_falls_to_estimate():
    r = resolve_grams(PLAIN, {"quantity_g": 120, "basis": "count", "servings": 2})
    assert (r["grams"], r["basis"], r["confidence"]) == (120, "estimate", "low")


def test_two_eggs_resolve_via_usda_units_not_a_guess():
    """The omelette case: the food has no serving_g, so "two eggs" used to
    collapse into a bare gram guess. One natural unit ("1 large" = 61g) resolves
    it properly."""
    egg = {"portions": EGG_PORTIONS, "serving_g": None}
    r = resolve_grams(egg, {"quantity_g": 100, "basis": "count", "servings": 2,
                            "count_verified": True})
    assert r["grams"] == pytest.approx(122.0)     # 2 x 61g, not the model's 100
    assert r["basis"] == "count" and r["confidence"] == "high"


def test_primary_unit_is_countable_not_the_biggest_measure():
    """"One" egg is "1 large" (61g), never "1 cup" (220g)."""
    assert primary_unit_grams({"portions": EGG_PORTIONS}) == pytest.approx(61.0)
    pot = {"portions": [{"unit": "medium", "qty": 1, "grams": 480.0, "desc": "1 medium"},
                        {"unit": "pot", "qty": 1, "grams": 1800.0,
                         "desc": "1 large pot (60 FO, 12 servings)"}]}
    assert primary_unit_grams(pot) == pytest.approx(480.0)
    assert primary_unit_grams({}) is None


def test_no_countable_unit_means_no_invented_count():
    """A food known only in cups and spoons has no "one of it" — better a flagged
    estimate than pretending a cup is an item."""
    spoons = {"portions": [{"unit": "cup", "qty": 1, "grams": 240.0, "desc": "1 cup"},
                           {"unit": "tbsp", "qty": 1, "grams": 15.0, "desc": "1 tbsp"}]}
    assert primary_unit_grams(spoons) is None
    r = resolve_grams({"serving_g": None, **spoons},
                      {"quantity_g": 200, "basis": "count", "servings": 2})
    assert r["basis"] == "estimate"


# ── Household phrasing for foods with no serving size ────────────────────────
def test_two_eggs_read_as_eggs_not_grams():
    """A bare USDA adjective must not be pluralised into "2 larges"."""
    assert portion_label(122.0, None, None, json.dumps(EGG_PORTIONS)) == "2 large"


def test_label_prefers_a_clean_count_over_a_bigger_unit():
    """122g is exactly 2 "large" but an awkward 0.55 of a "cup"."""
    label = portion_label(122.0, None, None, json.dumps(EGG_PORTIONS))
    assert "large" in label and label.startswith("2")


def test_label_renders_fractions():
    cups = [{"unit": "cup", "qty": 1, "grams": 200.0, "desc": "1 cup"}]
    assert portion_label(100.0, None, None, json.dumps(cups)) == "½ cup"
    assert portion_label(300.0, None, None, json.dumps(cups)) == "1½ cups"


def test_label_defers_when_serving_size_exists():
    """The client already renders "≈ 5 cakes" from serving_g; don't double up."""
    assert portion_label(45.0, 9.0, "1 cake", json.dumps(EGG_PORTIONS)) is None


def test_label_is_silent_when_nothing_fits():
    assert portion_label(5000.0, None, None, json.dumps(EGG_PORTIONS)) is None
    assert portion_label(100.0, None, None, None) is None
    assert portion_label(100.0, None, None, "not json") is None


# ── Rung 3: household measures ───────────────────────────────────────────────
def test_household_uses_usda_portion():
    r = resolve_grams(RICE, {"quantity_g": 600, "basis": "household",
                             "household_qty": 1, "household_unit": "cup"})
    assert r["grams"] == 158.0              # the Menu-Match fix: not 600g
    assert r["basis"] == "household" and r["confidence"] == "medium"


def test_household_scales_and_normalizes_plural():
    r = resolve_grams(RICE, {"quantity_g": 999, "basis": "household",
                             "household_qty": 2.5, "household_unit": "cups"})
    assert r["grams"] == 2.5 * 158.0


def test_weight_units_need_no_food_data():
    r = resolve_grams(PLAIN, {"quantity_g": 1, "basis": "household",
                              "household_qty": 4, "household_unit": "oz"})
    assert abs(r["grams"] - 4 * 28.35) < 0.01
    assert r["confidence"] == "high"


def test_volume_fallback_for_drinks():
    r = resolve_grams(PLAIN, {"quantity_g": 1, "basis": "household",
                              "household_qty": 12, "household_unit": "fl oz"})
    assert abs(r["grams"] - 12 * 29.57) < 0.01
    assert r["confidence"] == "medium"


def test_unknown_household_unit_falls_to_estimate():
    r = resolve_grams(PLAIN, {"quantity_g": 200, "basis": "household",
                              "household_qty": 1, "household_unit": "bowl"})
    assert (r["grams"], r["basis"], r["confidence"]) == (200, "estimate", "low")


# ── Rung 4 + nothing ─────────────────────────────────────────────────────────
def test_estimate_flagged_low():
    r = resolve_grams(PLAIN, {"quantity_g": 250, "basis": "estimate"})
    assert (r["grams"], r["confidence"]) == (250, "low")


def test_nothing_resolves():
    assert resolve_grams(PLAIN, {"basis": "estimate"})["grams"] == 0


# ── Basis verification (the model is an unreliable narrator about evidence) ──
def test_stated_number_detects_real_quantities():
    assert stated_number("150 grams of chicken")
    assert stated_number("three tacos")
    assert stated_number("half a pizza")


def test_bare_article_is_not_a_stated_count():
    """"an order of jasmine rice" names a container, not a count — this exact
    pattern is what inflated confidence across all 123 calibration items."""
    assert not stated_number("I had an order of jasmine rice")
    assert not stated_number("a side salad")
    assert not stated_number("")
    assert not stated_number(None)


def test_verify_label_only_in_photos():
    photo = verify_claims({"basis": "label", "quantity_g": 35}, "photo", None)
    voice = verify_claims({"basis": "label", "quantity_g": 35}, "voice", "beef jerky")
    assert photo["basis"] == "label"
    assert voice["basis"] == "estimate"
    assert voice["quantity_g"] == 35          # grams never change


def test_verify_stated_needs_a_number():
    vague = verify_claims({"basis": "stated", "quantity_g": 150}, "voice", "some chicken")
    exact = verify_claims({"basis": "stated", "quantity_g": 150}, "voice", "150g chicken")
    assert vague["basis"] == "estimate" and exact["basis"] == "stated"


def test_verify_marks_count_verification():
    counted = verify_claims({"basis": "count", "servings": 3}, "voice", "three tacos")
    guessed = verify_claims({"basis": "count", "servings": 1}, "voice", "an order of rice")
    assert counted["count_verified"] is True
    assert guessed["count_verified"] is False
    assert guessed["basis"] == "count"        # basis kept; only trust changes


# ── USDA foodPortions parsing ────────────────────────────────────────────────
def test_parse_fndds_portion_description():
    detail = {"foodPortions": [
        {"portionDescription": "1 cup, cooked", "gramWeight": 158.0},
        {"portionDescription": "Quantity not specified", "gramWeight": 100.0},
        {"portionDescription": "1 1/2 cups", "gramWeight": 237.0},  # dup unit: first wins
    ]}
    p = parse_usda_portions(detail)
    assert p == [{"unit": "cup", "qty": 1, "grams": 158.0, "desc": "1 cup, cooked"}]


def test_parse_sr_legacy_amount_modifier():
    detail = {"foodPortions": [
        {"amount": 1.0, "modifier": "tbsp", "gramWeight": 16.0,
         "measureUnit": {"name": "undetermined"}},
        {"amount": 1.0, "modifier": "", "gramWeight": 240.0,
         "measureUnit": {"name": "cup"}},
        {"amount": 1.0, "modifier": "slice", "gramWeight": 0},       # no grams: skipped
    ]}
    p = parse_usda_portions(detail)
    assert {x["unit"] for x in p} == {"tbsp", "cup"}


def test_parse_fraction_description():
    detail = {"foodPortions": [{"portionDescription": "1/2 cup", "gramWeight": 79.0}]}
    p = parse_usda_portions(detail)
    assert p[0]["qty"] == 0.5 and p[0]["grams"] == 79.0
    assert match_household(p, 2, "cup") == 2 * (79.0 / 0.5)


def test_match_household_no_match():
    assert match_household(RICE["portions"], 1, "slice") is None
    assert match_household(None, 1, "cup") is None


# ── Estimate snapping (down-only, blind guesses vs household reality) ────────
def test_snap_caps_overshoot():
    g, note = snap_estimate(RICE, 534)          # the Menu-Match jasmine case
    assert g == 2 * 158.0 and "capped" in note


def test_snap_leaves_plausible_estimates_alone():
    assert snap_estimate(RICE, 200) == (200, None)


def test_snap_never_raises_a_low_guess():
    assert snap_estimate(RICE, 50) == (50, None)


def test_snap_falls_back_to_serving_g_for_branded_rows():
    """Branded USDA rows have no foodPortions but DO have serving_g — a blind
    guess must not sail past the row's own serving size (the pizza case)."""
    g, note = snap_estimate(TACO, 900)               # portions None, serving_g 102
    assert g == 2 * 102.0 and "serving" in note


def test_snap_noop_without_any_anchor():
    assert snap_estimate(PLAIN, 900) == (900, None)  # no portions, no serving_g


def test_snap_normalizes_fractional_portion_qty():
    food = {"portions": [{"unit": "cup", "qty": 0.5, "grams": 79.0}]}
    g, note = snap_estimate(food, 500)
    assert g == 2 * (79.0 / 0.5)                # per-UNIT weight, not per-row


# ── Guard ────────────────────────────────────────────────────────────────────
def test_guard_caps_serving_multiple():
    g, note = guard_grams(TACO, 5000)
    assert g == 20 * 102.0 and "servings" in note


def test_guard_caps_absolute():
    g, note = guard_grams(PLAIN, 9000)
    assert g == 2500.0 and note


def test_guard_floor_and_passthrough():
    assert guard_grams(PLAIN, 0.2) == (1.0, None)
    assert guard_grams(TACO, 204.0) == (204.0, None)
