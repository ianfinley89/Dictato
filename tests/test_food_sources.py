"""The source registry must reproduce the behaviour it replaces, exactly.

`source` carried five orthogonal facts in six separate tuples across five
modules. Consolidating them is only safe if the derived sets are identical to
the ones that existed before — a refactor that quietly widens `private` leaks
one user's food to another, and one that quietly widens `editable` lets a user
rewrite shared reference data for everybody.

So the sets below are written out as literals, copied from the code as it stood
BEFORE the registry existed. They are the control. If a future edit to the
registry changes what a source may do, one of these fails and says which.
"""
from app.services import food_sources as fs


# ── The control: every set as it was, hard-coded ─────────────────────────────
# food_lookup._PRIVATE_SOURCES, and the inline list in _SCOPE_SQL
BEFORE_PRIVATE = ("user", "recipe", "estimate")
# food_lookup._AUTHORED_SOURCES
BEFORE_AUTHORED = ("user", "recipe")
# food_lookup._ESTIMATE_SOURCE
BEFORE_INVENTED = ("estimate",)
# nutrition_guard scoping, inline in _cache_food and _repair_nutrients
BEFORE_PER_100G = ("usda", "off")
# plausibility._TRUSTED and portion_class._TRUSTED (identical, defined twice)
BEFORE_REFERENCE_STATS = ("usda", "off")
# routers/foods.py update_food, and AI_MADE in app.js
BEFORE_EDITABLE = ("web", "estimate", "user")
# services/logging.py SOURCE_LABELS
BEFORE_LABELS = {
    "usda": "USDA", "off": "Open Food Facts", "fatsecret": "FatSecret",
    "user": "Custom (yours)", "manual": "Manual", "recipe": "Recipe",
    "estimate": "AI estimate", "web": "Web (published)",
}


def test_private_set_is_unchanged():
    """Widening this leaks one user's private food into another's search."""
    assert set(fs.PRIVATE) == set(BEFORE_PRIVATE)


def test_authored_set_is_unchanged():
    assert set(fs.AUTHORED) == set(BEFORE_AUTHORED)


def test_invented_set_is_unchanged():
    assert set(fs.INVENTED) == set(BEFORE_INVENTED)


def test_per_100g_set_is_unchanged():
    """Widening this lets the plausibility guard "correct" per-SERVING numbers.
    That is the bug that filed a batch recipe at 315 g of protein per 100 g."""
    assert set(fs.PER_100G) == set(BEFORE_PER_100G)


def test_reference_stats_set_is_unchanged():
    assert set(fs.REFERENCE_STATS) == set(BEFORE_REFERENCE_STATS)


def test_editable_set_is_unchanged():
    """Widening this lets one user rewrite shared reference data for everyone."""
    assert set(fs.EDITABLE) == set(BEFORE_EDITABLE)


def test_labels_are_unchanged():
    for key, text in BEFORE_LABELS.items():
        assert fs.label(key) == text, key


def test_every_source_in_the_schema_is_declared():
    """CLAUDE.md's data model lists these eight. A source in the database but
    not in the registry silently falls back to the conservative default."""
    assert set(fs.SOURCES) == {"usda", "off", "fatsecret", "manual",
                               "user", "recipe", "web", "estimate"}


# ── The properties the sets exist to express ─────────────────────────────────
def test_authority_order_is_authored_then_real_then_invented():
    """The order hard rule #1 turns on, and the one that was never declared:
    an invented food must never outrank measured data."""
    assert fs.trust("recipe") < fs.trust("usda") < fs.trust("estimate")
    assert fs.trust("user") < fs.trust("fatsecret")
    assert fs.trust("web") < fs.trust("estimate")


def test_estimate_is_private_but_not_authored():
    """Exactly the conflation that inverted the rule: `estimate` belongs with
    user foods for PRIVACY and with nothing for PRECEDENCE."""
    assert "estimate" in fs.PRIVATE
    assert "estimate" not in fs.AUTHORED
    assert fs.trust("estimate") == fs.TRUST_INVENTED


def test_nothing_learns_statistics_from_a_guess():
    """Learning typical servings from the model's own estimates is circular."""
    for key in fs.INVENTED + fs.AUTHORED:
        assert key not in fs.REFERENCE_STATS, key


def test_shared_reference_rows_are_never_editable():
    for key in ("usda", "off", "fatsecret"):
        assert not fs.get(key).editable, key


def test_is_authored_by_matches_the_old_helper():
    uid = 7
    assert fs.is_authored_by({"source": "recipe", "created_by_user_id": uid}, uid) is True
    assert fs.is_authored_by({"source": "user", "created_by_user_id": uid}, uid) is True
    assert fs.is_authored_by({"source": "estimate", "created_by_user_id": uid}, uid) is False
    assert fs.is_authored_by({"source": "usda", "created_by_user_id": None}, uid) is False
    # Someone else's recipe is not yours.
    assert fs.is_authored_by({"source": "recipe", "created_by_user_id": 99}, uid) is False


def test_unknown_source_gets_no_privileges():
    """A source added to the database but not here must not silently become
    editable, private, or something we learn from."""
    u = fs.get("some-new-source")
    assert not u.editable and not u.private and not u.reference_stats
    assert not u.per_100g and u.trust == fs.TRUST_REFERENCE
    assert fs.label("some-new-source") == "Some-New-Source"


# ── The one copy that cannot import the registry ─────────────────────────────
def test_client_editable_set_matches_the_server():
    """The PWA keeps its own literal (eight constants are not worth an
    endpoint). If the two drift, the UI offers a "Fix name" button that the
    server then rejects with a 403, which is invisible until a user hits it."""
    import re
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const AI_MADE = \[([^\]]*)\];", js)
    assert m, "AI_MADE not found in app.js — did it move?"
    client = {x.strip().strip("'\"") for x in m.group(1).split(",")}
    assert client == set(fs.EDITABLE)
