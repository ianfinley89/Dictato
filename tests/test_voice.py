import pytest
from app.services.voice_parse import parse_local
from app.database import get_conn


# ── Local parser unit tests ───────────────────────────────────────────────────

def test_parse_explicit_grams():
    items = parse_local("100g chicken breast")
    assert len(items) == 1
    assert items[0]["est_quantity_g"] == pytest.approx(100.0)
    assert "chicken" in items[0]["name"]
    assert items[0]["confidence"] >= 0.9


def test_parse_with_prefix():
    items = parse_local("I ate 200 grams of white rice")
    assert len(items) == 1
    assert items[0]["est_quantity_g"] == pytest.approx(200.0)
    assert "rice" in items[0]["name"]


def test_parse_count_word():
    items = parse_local("one quaker rice cake")
    assert len(items) == 1
    assert "rice cake" in items[0]["name"]
    assert items[0]["confidence"] == pytest.approx(0.5)


def test_parse_multiple_items():
    items = parse_local("I had two eggs and a banana")
    assert len(items) == 2
    names = [i["name"] for i in items]
    assert any("egg" in n for n in names)
    assert any("banana" in n for n in names)


def test_parse_tbsp():
    items = parse_local("1 tbsp peanut butter")
    assert len(items) == 1
    assert items[0]["est_quantity_g"] == pytest.approx(15.0)
    assert items[0]["confidence"] >= 0.7


def test_parse_bare_name():
    items = parse_local("oatmeal")
    assert len(items) == 1
    assert items[0]["name"] == "oatmeal"
    assert items[0]["confidence"] == pytest.approx(0.3)
    assert items[0]["est_quantity_g"] == pytest.approx(100.0)


def test_parse_extracts_brand():
    items = parse_local("I had a steak burrito from chipotle")
    assert items[0]["name"] == "steak burrito"
    assert items[0]["brand"] == "chipotle"


def test_parse_brand_ignores_from_the():
    # "from the grill" / "from scratch" are not brands
    items = parse_local("chicken from the grill")
    assert items[0]["brand"] is None
    assert "grill" in items[0]["name"]


def test_parse_no_brand():
    items = parse_local("100g chicken breast")
    assert items[0]["brand"] is None


def test_parse_container_of_food():
    # "a bowl of stew" → food is "stew" (not "bowl of stew"), one serving
    items = parse_local("I ate a bowl of stew")
    assert len(items) == 1
    assert items[0]["name"] == "stew"
    assert items[0]["est_servings"] == pytest.approx(1.0)


def test_parse_two_glasses_of_milk():
    items = parse_local("two glasses of milk")
    assert items[0]["name"] == "milk"
    assert items[0]["est_servings"] == pytest.approx(2.0)


def test_parse_emits_servings_for_count():
    # "a dr pepper" → 1 serving, article stripped from the name
    items = parse_local("I had a dr pepper")
    assert len(items) == 1
    assert items[0]["name"] == "dr pepper"
    assert items[0]["est_servings"] == pytest.approx(1.0)


def test_parse_half_serving():
    items = parse_local("I had half a dr pepper")
    assert len(items) == 1
    assert items[0]["name"] == "dr pepper"        # leading article removed
    assert items[0]["est_servings"] == pytest.approx(0.5)


def test_parse_grams_has_no_servings():
    items = parse_local("100g chicken breast")
    assert items[0]["est_servings"] is None


def test_parse_strips_filler_and_punctuation():
    # Real spoken phrase: filler words + punctuation should be cleaned from the
    # name so the food lookup gets a tidy query.
    items = parse_local("I had two rice cakes today, whole ones.")
    assert len(items) == 1
    name = items[0]["name"]
    assert "today" not in name
    assert "." not in name and "," not in name
    assert "rice cakes" in name
    assert items[0]["est_quantity_g"] == pytest.approx(200.0)  # 2 x count default


# Endpoint tests for voice/photo logging live in test_agent.py — the old
# /api/voice/parse and /api/photo/parse routes were replaced by /api/agent/log.
