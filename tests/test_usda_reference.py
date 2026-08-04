"""The locally imported USDA reference: household measures without an API call.

`ensure_portions` used to fetch a food's measures the first time something needed
them, one network call per food, which meant almost nothing had them — 23 of 1331
cached foods. USDA publishes the whole thing as CSV, so import it once: a food is
anchored the moment it is cached, offline, forever.

What is tested here is the seam. The importer must produce byte-identical output
to the live API path (same parser, no second dialect), and the lookup must prefer
the local table so no network call is made for a food we already have.
"""
import importlib.util
import io
import json
import os
import sys
import zipfile

import pytest

from app.database import get_conn
from app.services import food_lookup
from app.services.portion import parse_usda_portions

_spec = importlib.util.spec_from_file_location(
    "import_usda_reference",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "import_usda_reference.py"))
importer = importlib.util.module_from_spec(_spec)
sys.modules["import_usda_reference"] = importer
_spec.loader.exec_module(importer)


def _zip(**files: str) -> zipfile.ZipFile:
    """A stand-in for a USDA dataset zip, nested one directory deep like the real
    downloads."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(f"FoodData_Central_2024-10-31/{name}.csv", body)
    return zipfile.ZipFile(buf)


PORTION_HEADER = ("id,fdc_id,seq_num,amount,measure_unit_id,portion_description,"
                  "modifier,gram_weight,data_points,footnote,min_year_acquired\n")
UNITS = "id,name\n1000,cup\n1038,oz\n1050,slice\n1011,paired raw w\n9999,undetermined\n"


# ── The three dialects in one set of columns ─────────────────────────────────
def test_importer_reads_all_three_dataset_dialects():
    """FNDDS puts the measure in `portion_description` and a numeric CODE in
    `modifier`; SR Legacy puts free text in `modifier`; Foundation uses
    `measure_unit_id`. All three arrive in the same columns."""
    z = _zip(measure_unit=UNITS, food_portion=PORTION_HEADER + "\n".join([
        "1,111,1,,9999,1 cup,10205,246.0,,,",              # FNDDS
        "2,222,1,1,9999,,slice,28.0,,,",                    # SR Legacy
        "3,333,1,2.0,1038,,,35.8,,,",                       # Foundation
    ]))
    got = importer._portions(z)
    assert got["111"] == [{"unit": "cup", "qty": 1.0, "grams": 246.0, "desc": "1 cup"}]
    assert got["222"] == [{"unit": "slice", "qty": 1.0, "grams": 28.0, "desc": "1 slice"}]
    assert got["333"] == [{"unit": "oz", "qty": 2.0, "grams": 35.8, "desc": "2 oz"}]


def test_importer_output_matches_the_api_parser_exactly():
    """One parser, so a food imported in bulk and the same food fetched live can
    never disagree about what it weighs."""
    z = _zip(measure_unit=UNITS, food_portion=PORTION_HEADER + "\n".join([
        "1,111,1,,9999,1 cup,10205,246.0,,,",
        "2,111,2,,9999,Quantity not specified,90000,244.0,,,",
        "3,111,3,,9999,1 fl oz,30000,30.8,,,",
    ]))
    api_shape = {"foodPortions": [
        {"amount": None, "portionDescription": "1 cup", "modifier": "10205",
         "gramWeight": 246.0, "measureUnit": {"name": "undetermined"}},
        {"amount": None, "portionDescription": "Quantity not specified",
         "modifier": "90000", "gramWeight": 244.0, "measureUnit": {"name": "undetermined"}},
        {"amount": None, "portionDescription": "1 fl oz", "modifier": "30000",
         "gramWeight": 30.8, "measureUnit": {"name": "undetermined"}},
    ]}
    assert importer._portions(z)["111"] == parse_usda_portions(api_shape)


def test_importer_drops_codes_and_qualifiers():
    """A portion code and a qualifier are not measures — rendered literally they
    become "1 90000" and "1 paired raw w"."""
    z = _zip(measure_unit=UNITS, food_portion=PORTION_HEADER + "\n".join([
        "1,111,1,1,9999,,90000,18.0,,,",
        "2,111,2,1,1011,,,87.0,,,",
        "3,111,3,1,9999,,with skin,346.0,,,",
        "4,111,4,1,1050,,,28.0,,,",
    ]))
    assert importer._portions(z)["111"] == [
        {"unit": "slice", "qty": 1.0, "grams": 28.0, "desc": "1 slice"}]


def test_importer_skips_zero_and_unparseable_gram_weights():
    z = _zip(measure_unit=UNITS, food_portion=PORTION_HEADER + "\n".join([
        "1,111,1,1,1050,,,0.0,,,",
        "2,111,2,1,1050,,,not-a-number,,,",
        "3,222,1,1,1000,,,240.0,,,",
    ]))
    got = importer._portions(z)
    assert "111" not in got                    # nothing usable — no empty row
    assert got["222"][0]["grams"] == 240.0


# ── Composition ──────────────────────────────────────────────────────────────
COMP_HEADER = "id,fdc_id,fdc_id_of_input_food,seq_num,amount,sr_description,gram_weight\n"


def test_composition_carries_fractions_not_just_grams():
    """The fractions are the point: estimate the dish's mass ONCE and distribute
    it, rather than guessing every ingredient separately and multiplying the
    portion error."""
    z = _zip(input_food=COMP_HEADER + "\n".join([
        "1,111,1,1,1,Beef patty,80.0",
        "2,111,2,2,1,Bun,50.0",
        "3,111,3,3,1,Cheese,20.0",
    ]))
    parts = importer._composition(z)["111"]
    assert [p["name"] for p in parts] == ["beef patty", "bun", "cheese"]   # heaviest first
    assert parts[0]["fraction"] == 0.5333
    assert sum(p["fraction"] for p in parts) == pytest.approx(1.0, abs=0.001)


def test_composition_ignores_single_ingredient_foods():
    """One "ingredient" is a synonym, not a recipe — decomposing it buys nothing
    and invites a pointless second guess."""
    z = _zip(input_food=COMP_HEADER + "1,111,1,1,1,Whole milk,100.0")
    assert importer._composition(z) == {}


# ── The lookup seam ──────────────────────────────────────────────────────────
def _usda_food(source_id: str, portions_json=None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, source_id, name, nutrients_json, portions_json)
               VALUES ('usda', ?, 'test food', '{"calories": 100.0}', ?)""",
            (source_id, portions_json))
        return dict(conn.execute("SELECT * FROM foods WHERE id=?", (cur.lastrowid,)).fetchone())


def _reference(fdc_id: str, portions: list) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO usda_portions (fdc_id, portions_json) VALUES (?,?)",
                     (fdc_id, json.dumps(portions)))


CUP = [{"unit": "cup", "qty": 1, "grams": 240.0, "desc": "1 cup"}]


@pytest.mark.anyio
async def test_ensure_portions_uses_the_local_table_without_a_network_call(client, monkeypatch):
    _reference("55501", CUP)
    food = _usda_food("55501")
    food["portions"] = None

    def boom(*a, **k):                       # any HTTP client construction fails
        raise AssertionError("hit the network for a food we already have")
    monkeypatch.setattr(food_lookup.httpx, "AsyncClient", boom)

    got = await food_lookup.ensure_portions(food)
    assert got["portions"] == CUP
    with get_conn() as conn:                 # and it is persisted, not just returned
        row = conn.execute("SELECT portions_json FROM foods WHERE id=?", (food["id"],)).fetchone()
    assert json.loads(row["portions_json"]) == CUP


@pytest.mark.anyio
async def test_ensure_portions_leaves_unknown_foods_for_the_api(client, monkeypatch):
    """A Branded food is not in the published datasets — the lazy fetch must
    still be the fallback, not be short-circuited into "no measures"."""
    food = _usda_food("99999")
    food["portions"] = None
    monkeypatch.setattr(food_lookup, "USDA_API_KEY", "")     # no key: give up quietly
    got = await food_lookup.ensure_portions(food)
    assert got.get("portions") is None


def test_newly_cached_food_arrives_anchored(client):
    """The whole point: no waiting for the first log that needs a measure."""
    _reference("55502", CUP)
    fid = food_lookup._cache_food({
        "source": "usda", "source_id": "55502", "name": "cached food",
        "nutrients_json": json.dumps({"calories": 100.0, "protein_g": 1.0,
                                      "carbs_g": 1.0, "fat_g": 1.0})})
    assert food_lookup.get_food_by_id(fid)["portions"] == CUP
