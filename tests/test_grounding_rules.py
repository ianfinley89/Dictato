"""Hard rule #1 enforced in CODE, not merely asked for in the prompt.

The Menu-Match eval caught the model searching "meat lasagna", receiving six
clean USDA rows, and then calling create_food anyway with brand="Italian
Restaurant" — inventing nutrition for a dish the database already had, and
getting it labelled `web` although no web_search ever ran. Prompt rules did not
prevent it; these guards do.
"""
import asyncio
import json

import pytest

from app.database import get_conn
from app.services.agent import _tool_create_food, _real_brand

REG = {"email": "ground@example.com", "password": "password123", "display_name": "G"}


def _register(client) -> int:
    return client.post("/api/auth/register", json=REG).json()["user_id"]


def _seed(name: str, source: str = "usda", serving_g: float | None = 255.0) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, name, serving_g, nutrients_json)
               VALUES (?,?,?,?)""",
            (source, name, serving_g,
             json.dumps({"calories": 161.0, "protein_g": 11.0, "carbs_g": 15.0, "fat_g": 6.0})))
        return cur.lastrowid


_NUMBERS = {"values_per": "serving", "serving_g": 457, "calories": 845,
            "protein_g": 40, "carbs_g": 70, "fat_g": 40}


# ── "brand" must name an actual brand ────────────────────────────────────────
@pytest.mark.parametrize("brand", ["Italian Restaurant", "restaurant", "a diner",
                                   "homemade", "the local place", "", None, "Various"])
def test_generic_descriptors_are_not_brands(brand):
    assert _real_brand(brand) is None


@pytest.mark.parametrize("brand", ["Oberto", "Trader Joe's", "Chipotle", "Nurri"])
def test_real_brands_survive(brand):
    assert _real_brand(brand) == brand.lower()


# ── The leak itself ──────────────────────────────────────────────────────────
def test_create_food_refuses_to_duplicate_a_known_dish(client):
    uid = _register(client)
    existing = _seed("Meat Lasagna")
    out = asyncio.run(_tool_create_food(uid, {
        "name": "Meat Lasagna", "brand": "Italian Restaurant",
        "basis": "estimate", **_NUMBERS}))
    assert "error" in out and out["use_food_id"] == existing
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM foods WHERE name='meat lasagna'").fetchone()["c"]
    assert n == 0                       # nothing invented


def test_plural_and_case_variants_are_still_the_same_dish(client):
    uid = _register(client)
    _seed("Breadsticks")
    out = asyncio.run(_tool_create_food(uid, {
        "name": "breadstick", "basis": "estimate", **_NUMBERS}))
    assert "error" in out


def test_a_genuinely_new_branded_product_is_still_created(client):
    """The Oberto case: a generic 'Beef Jerky' row must NOT block a branded
    product whose label the model actually read — that create made the numbers
    exact rather than invented."""
    uid = _register(client)
    _seed("Beef Jerky", serving_g=28.0)
    out = asyncio.run(_tool_create_food(uid, {
        "name": "oberto original beef jerky", "brand": "Oberto",
        "basis": "label", "values_per": "serving", "serving_g": 35,
        "calories": 90, "protein_g": 14, "carbs_g": 9, "fat_g": 0.5}))
    assert out.get("created") is True


# ── The source label has to be true ──────────────────────────────────────────
def test_web_label_requires_a_citation(client):
    uid = _register(client)
    no_url = asyncio.run(_tool_create_food(uid, {
        "name": "mystery restaurant bowl", "brand": "Chipotle",
        "basis": "web", **_NUMBERS}))
    assert no_url["source"] == "estimate"        # claimed web, cited nothing

    cited = asyncio.run(_tool_create_food(uid, {
        "name": "another restaurant bowl", "brand": "Chipotle", "basis": "web",
        "source_url": "https://example.com/nutrition", **_NUMBERS}))
    assert cited["source"] == "web"


def test_user_and_estimate_rows_do_not_block_creation(client):
    """A previous AI estimate is not authority over a fresh lookup."""
    uid = _register(client)
    _seed("Mystery Stew", source="estimate")
    out = asyncio.run(_tool_create_food(uid, {
        "name": "Mystery Stew", "basis": "estimate", **_NUMBERS}))
    assert out.get("created") is True
