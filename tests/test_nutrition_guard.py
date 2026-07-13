"""The nutrition plausibility guard: catches the kJ-as-kcal ingest bug and
physically impossible energy before a foods row is stored. Pure function — no
DB, no model."""
import pytest

from app.services.nutrition_guard import sanitize_per_100g


def test_real_food_is_left_alone():
    # 80/20 ground beef, honest kcal — energy ≈ Atwater, nothing to fix.
    beef = {"calories": 254.0, "protein_g": 17.0, "carbs_g": 0.0, "fat_g": 20.0}
    clean, note = sanitize_per_100g(beef)
    assert note is None
    assert clean["calories"] == pytest.approx(254.0)


def test_kj_ground_beef_is_converted():
    # The bug: 1046 kJ stored as "1046 calories". Atwater ≈ 248, ratio ≈ 4.22.
    clean, note = sanitize_per_100g(
        {"calories": 1046.0, "protein_g": 17.0, "carbs_g": 0.0, "fat_g": 20.0}
    )
    assert note and "kilojoule" in note
    assert clean["calories"] == pytest.approx(250.0, abs=2)


def test_kj_olive_oil_is_converted():
    # 3700 kJ of oil read as kcal; Atwater = 900 (100g fat), ratio ≈ 4.11.
    clean, note = sanitize_per_100g(
        {"calories": 3700.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 100.0}
    )
    assert note
    assert clean["calories"] == pytest.approx(884.0, abs=2)


def test_impossible_energy_recomputed_from_macros():
    # 5000 kcal is not a kJ match (ratio ≈ 20) but is physically impossible;
    # macros are sane, so recompute from them.
    clean, note = sanitize_per_100g(
        {"calories": 5000.0, "protein_g": 10.0, "carbs_g": 20.0, "fat_g": 30.0}
    )
    assert note and "recomputed" in note
    assert clean["calories"] == pytest.approx(4 * 10 + 4 * 20 + 9 * 30)  # 390


def test_impossible_energy_capped_when_macros_are_garbage():
    clean, note = sanitize_per_100g(
        {"calories": 9999.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    )
    assert note and "capped" in note
    assert clean["calories"] == 900.0


def test_missing_energy_computed_from_macros():
    clean, note = sanitize_per_100g(
        {"calories": 0.0, "protein_g": 5.0, "carbs_g": 20.0, "fat_g": 3.0}
    )
    assert note and "computed from macros" in note
    assert clean["calories"] == pytest.approx(4 * 5 + 4 * 20 + 9 * 3)  # 127


def test_alcohol_spirit_not_touched():
    # Vodka: ~231 kcal/100g from alcohol, ~0 macros. Atwater ≈ 0, so the guard
    # must NOT "correct" the legitimately-high energy.
    clean, note = sanitize_per_100g(
        {"calories": 231.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    )
    assert note is None
    assert clean["calories"] == pytest.approx(231.0)


def test_diet_soda_zero_calories_stays_zero():
    clean, note = sanitize_per_100g(
        {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    )
    assert note is None
    assert clean["calories"] == 0.0


def test_extra_keys_preserved():
    clean, _ = sanitize_per_100g(
        {"calories": 254.0, "protein_g": 17.0, "carbs_g": 0.0, "fat_g": 20.0,
         "fiber_g": 0.0, "micros": {"sodium_mg": 70}}
    )
    assert clean["micros"] == {"sodium_mg": 70}


def test_none_input_is_safe():
    clean, note = sanitize_per_100g(None)
    assert note is None
    assert clean["calories"] == 0.0
