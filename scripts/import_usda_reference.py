"""Import USDA household measures and recipe composition once, locally.

`ensure_portions` fetches a food's measures from the API the first time something
needs them, which meant almost nothing had them: 23 of 1331 cached foods. A food
met for the first time therefore had no gram weight for a count ("two eggs"), no
household measure, and nothing to bound a wild guess — until an hourly job caught
up an hour later.

USDA publishes the whole thing: survey (FNDDS), SR Legacy and Foundation are 3-6
MB each, 47k portion rows and 24k composition rows between them. Importing once
means every one of those foods is anchored the moment it is cached, with no API
call ever.

Two tables:
  usda_portions    fdc_id -> the same normalized shape ensure_portions produces
  usda_composition fdc_id -> what the dish is MADE OF, with gram weights

    uv run python scripts/import_usda_reference.py            # uses data/usda/*.zip
    uv run python scripts/import_usda_reference.py --download  # fetch them first
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict

sys.path.insert(0, ".")

from app.database import get_conn, init_db             # noqa: E402
from app.services.portion import parse_usda_portions   # noqa: E402

USDA_DIR = os.path.join("data", "usda")
SETS = {
    "survey": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_survey_food_csv_2024-10-31.zip",
    "sr": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip",
    "foundation": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2024-10-31.zip",
}


def _open(z: zipfile.ZipFile, filename: str):
    match = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == filename), None)
    if not match:
        return None
    return csv.DictReader(io.TextIOWrapper(z.open(match), encoding="utf-8", errors="replace"))


def _units(z: zipfile.ZipFile) -> dict:
    rd = _open(z, "measure_unit.csv")
    return {r["id"]: (r.get("name") or "").strip() for r in rd} if rd else {}


def _portions(z: zipfile.ZipFile) -> dict:
    """fdc_id -> [{unit, qty, grams, desc}].

    The CSV columns hold three different conventions — FNDDS puts the whole
    measure in `portion_description` ("1 cup") and a numeric code in `modifier`,
    SR Legacy puts free text in `modifier` ("waffle, square"), Foundation uses
    `measure_unit_id`. `parse_usda_portions` already untangles all three for the
    API, so rebuild the API's shape and hand it over: one parser, so a food
    imported here and a food fetched live can never disagree."""
    units = _units(z)
    rd = _open(z, "food_portion.csv")
    if not rd:
        return {}
    raw = defaultdict(list)
    for r in rd:
        try:
            grams = float(r.get("gram_weight") or 0)
        except ValueError:
            continue
        if grams <= 0:
            continue
        try:
            amount = float(r["amount"]) if (r.get("amount") or "").strip() else None
        except ValueError:
            amount = None
        raw[r["fdc_id"]].append({
            "gramWeight": grams,
            "portionDescription": r.get("portion_description") or "",
            "amount": amount,
            "modifier": r.get("modifier") or "",
            "measureUnit": {"name": units.get(r.get("measure_unit_id") or "", "")},
        })
    out = {}
    for fdc, ps in raw.items():
        parsed = parse_usda_portions({"foodPortions": ps})
        if parsed:
            out[fdc] = parsed
    return out


def _composition(z: zipfile.ZipFile) -> dict:
    """fdc_id -> [{name, grams, fraction}] — what the dish is made of.

    Lets "no cheese" become a real operation while the calorie total still rests
    on ONE mass judgement: estimate the dish, distribute by these fractions.
    Decomposing into independently-guessed parts would multiply the portion
    error, which is already the dominant one."""
    rd = _open(z, "input_food.csv")
    if not rd:
        return {}
    parts = defaultdict(list)
    for r in rd:
        name = (r.get("sr_description") or r.get("ingredient_description") or "").strip()
        try:
            grams = float(r.get("gram_weight") or 0)
        except ValueError:
            continue
        if not name or grams <= 0:
            continue
        parts[r["fdc_id"]].append({"name": name.lower(), "grams": round(grams, 2)})
    out = {}
    for fdc, items in parts.items():
        total = sum(p["grams"] for p in items)
        if total <= 0 or len(items) < 2:      # a single "ingredient" is not a recipe
            continue
        out[fdc] = [{**p, "fraction": round(p["grams"] / total, 4)}
                    for p in sorted(items, key=lambda p: -p["grams"])][:12]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="fetch the zips first")
    args = ap.parse_args()
    os.makedirs(USDA_DIR, exist_ok=True)
    init_db()          # the reference tables may not exist yet

    if args.download:
        import httpx
        for name, url in SETS.items():
            path = os.path.join(USDA_DIR, f"{name}.zip")
            if os.path.exists(path):
                continue
            print(f"downloading {name} …")
            with httpx.Client(timeout=300, follow_redirects=True) as c:
                open(path, "wb").write(c.get(url).content)

    all_portions, all_comp = {}, {}
    for name in SETS:
        path = os.path.join(USDA_DIR, f"{name}.zip")
        if not os.path.exists(path):
            print(f"missing {path} — run with --download")
            continue
        z = zipfile.ZipFile(path)
        p, c = _portions(z), _composition(z)
        all_portions.update(p)
        all_comp.update(c)
        print(f"{name:11s} portions for {len(p):6d} foods   composition for {len(c):5d}")

    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO usda_portions (fdc_id, portions_json) VALUES (?,?)",
            [(k, json.dumps(v)) for k, v in all_portions.items()])
        conn.executemany(
            "INSERT OR REPLACE INTO usda_composition (fdc_id, parts_json) VALUES (?,?)",
            [(k, json.dumps(v)) for k, v in all_comp.items()])
        # Anchor everything already cached, immediately.
        filled = conn.execute(
            """UPDATE foods SET portions_json = (
                   SELECT p.portions_json FROM usda_portions p WHERE p.fdc_id = foods.source_id)
               WHERE source = 'usda' AND source_id IS NOT NULL
                 AND portions_json IS NULL
                 AND EXISTS (SELECT 1 FROM usda_portions p WHERE p.fdc_id = foods.source_id)"""
        ).rowcount

    print(f"\nimported {len(all_portions)} portion sets, {len(all_comp)} recipes")
    print(f"back-filled {filled} already-cached foods")


if __name__ == "__main__":
    main()
