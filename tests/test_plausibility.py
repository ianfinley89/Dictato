"""Sanity-checking invented nutrition against foods of the same kind.

A Vietnamese soup logged 92g of protein and nothing questioned it, because
nothing downstream knew what a soup looks like. It knows now — from our own
cached USDA/OFF rows, not from hard-coded tables.

The rule is WARN, never rewrite: a protein-fortified soup can genuinely sit
high, and silently editing the user's diary would be worse than an odd number.
The single exception is a provable unit mix-up.
"""
import json

import pytest

from app.database import get_conn
from app.services.plausibility import check_nutrition, neighbour_bands, _class_tokens


def _seed(name: str, cal: float, protein: float, carbs: float, fat: float,
          source: str = "usda") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO foods (source, name, nutrients_json) VALUES (?,?,?)",
            (source, name, json.dumps({"calories": cal, "protein_g": protein,
                                       "carbs_g": carbs, "fat_g": fat, "fiber_g": 0.0})))


def _seed_soups(client, n: int = 10) -> None:
    client.post("/api/auth/register", json={"email": "p@example.com",
                                            "password": "password123", "display_name": "P"})
    # Real classes vary; identical rows would make any spread look extreme.
    for i in range(n):
        _seed(f"Chicken Noodle Soup {i}", 55 + i * 4, 2.0 + i * 0.4, 6.0 + i * 0.5,
              0.8 + i * 0.35)


# ── Which word names the class ───────────────────────────────────────────────
def test_class_word_is_head_final():
    """English food names put the class last: "... glass noodle SOUP"."""
    assert _class_tokens("Vietnamese chicken glass noodle soup")[0] == "soup"
    assert _class_tokens("Kodiak protein pancakes")[0] == "pancakes"


def test_marketing_words_are_not_a_class():
    assert "organic" not in _class_tokens("organic fresh natural soup")
    assert "protein" not in _class_tokens("Kodiak protein pancakes")


# ── Saying nothing when we don't know ────────────────────────────────────────
def test_silent_without_enough_neighbours(client):
    """Two soups are not a distribution. Better silent than confidently wrong."""
    client.post("/api/auth/register", json={"email": "p@example.com",
                                            "password": "password123", "display_name": "P"})
    _seed("Tomato Soup", 60, 2.0, 8.0, 1.0)
    assert neighbour_bands("chicken soup") is None
    assert check_nutrition("chicken soup", {"calories": 3000, "protein_g": 300,
                                            "carbs_g": 100, "fat_g": 100}) is None


def test_ai_rows_cannot_validate_other_ai_rows(client):
    """Neighbours come only from usda/off — letting invented rows set the band
    would let bad data justify more bad data."""
    client.post("/api/auth/register", json={"email": "p@example.com",
                                            "password": "password123", "display_name": "P"})
    for i in range(12):
        _seed(f"Mystery Soup {i}", 2000, 200, 100, 90, source="estimate")
    assert neighbour_bands("mystery soup") is None


# ── Normal food stays silent ─────────────────────────────────────────────────
def test_a_normal_soup_is_not_flagged(client):
    _seed_soups(client)
    assert check_nutrition("vietnamese chicken soup",
                           {"calories": 96, "protein_g": 5.3, "carbs_g": 9.4, "fat_g": 3.5}) is None


def test_high_protein_is_normal_for_the_right_class(client):
    """35g of protein is alarming in a soup and unremarkable in jerky — the class
    is what makes a number plausible."""
    client.post("/api/auth/register", json={"email": "p@example.com",
                                            "password": "password123", "display_name": "P"})
    for i in range(10):
        _seed(f"Beef Jerky {i}", 400 + i, 33.0 + i * 0.5, 11.0, 8.0)
    assert check_nutrition("beef jerky",
                           {"calories": 410, "protein_g": 35.0, "carbs_g": 11.0,
                            "fat_g": 8.0}) is None


# ── Flagging, and the one provable error ─────────────────────────────────────
def test_absurd_value_is_flagged(client):
    _seed_soups(client)
    w = check_nutrition("chicken soup", {"calories": 300, "protein_g": 40,
                                         "carbs_g": 10, "fat_g": 12})
    assert w is not None
    assert "soup" in w["message"] and "40" in w["message"]
    assert w["likely_per_serving"] is False       # no mechanical explanation


def test_per_serving_mixup_is_detected(client):
    """Numbers that land in the class band once divided by the serving weight are
    per-serving values filed as per-100g — that is checkable, not guesswork."""
    _seed_soups(client)
    w = check_nutrition("chicken soup",
                        {"calories": 3000, "protein_g": 150, "carbs_g": 350, "fat_g": 75},
                        serving_g=5000)
    assert w and w["likely_per_serving"] is True
    assert "values_per='serving'" in w["message"]


def test_the_message_names_the_worst_field(client):
    _seed_soups(client)
    w = check_nutrition("chicken soup", {"calories": 70, "protein_g": 4.0,
                                         "carbs_g": 8.0, "fat_g": 60})
    assert w["message"].startswith("fat")


# ── Wiring: warn but still create; refuse only the provable mix-up ───────────
def test_create_food_warns_but_still_creates(client):
    import asyncio
    from app.services.agent import _tool_create_food
    _seed_soups(client)
    uid = 1
    warnings: dict = {}
    out = asyncio.run(_tool_create_food(uid, {
        "name": "grandma's chicken soup", "values_per": "100g", "basis": "estimate",
        "calories": 300, "protein_g": 40, "carbs_g": 10, "fat_g": 12}, warnings))
    assert out.get("created") is True          # never blocks the log
    assert "warning" in out and warnings       # but the user is told
