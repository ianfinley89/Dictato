"""Find dishes no database row actually IS — the test set for the recipe path.

A homemade dish can share a name with a standard one and share nothing else.
Someone logged "chickpea curry", then said theirs used blended tomatoes rather
than coconut milk; the nearest row is the coconut-milk version and no amount of
re-ranking finds a row that does not exist. The honest answer is a recipe built
from DB-grounded ingredients — which the agent currently cannot build.

Before building that, this collects the cases it would have to handle, from
three corpora we already have:

  captures      what our users actually said (the real distribution, small)
  menumatch     41 named restaurant dishes with dietitian calories
  fndds         composite dish names USDA itself decomposes into ingredients

A dish counts as a candidate when `strong_db_match` — the same function that
enforces hard rule #1 at create time — finds nothing that IS that dish. That is
the honest definition: not "search returned little", but "nothing in any database
is this food", which is exactly when a recipe is the only truthful option.

Runs against a scratch DB and hits USDA/OFF/FatSecret, never the live database.

    uv run python scripts/find_recipe_candidates.py
    uv run python scripts/find_recipe_candidates.py --source menumatch --limit 20
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, ".")

EVAL_DIR = os.path.join("data", "evals")
DB_PATH = os.path.join(EVAL_DIR, "recipe_candidates.db")
OUT_PATH = os.path.join(EVAL_DIR, "recipe_candidates.jsonl")
os.makedirs(EVAL_DIR, exist_ok=True)
os.environ.setdefault("DATABASE_PATH", DB_PATH)
os.environ.setdefault("WHISPER_WARMUP", "false")

LIVE_DB = os.path.join("data", "dictato.db")


def _from_captures() -> list[tuple[str, str]]:
    """Meal labels of captures our own detector flagged, plus their transcripts."""
    from app.services.recipe_candidate import is_candidate
    if not os.path.exists(LIVE_DB):
        return []
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out = []
    for r in c.execute("""SELECT transcript, meal_label, entries_json, tags_json
                          FROM capture_log WHERE transcript IS NOT NULL AND transcript != ''"""):
        corr = [t for t in json.loads(r["tags_json"] or "[]") if t.startswith("correction:")]
        if not is_candidate(r["transcript"], json.loads(r["entries_json"] or "[]"), corr):
            continue
        for e in json.loads(r["entries_json"] or "[]"):
            if e.get("food_name"):
                out.append((e["food_name"], r["transcript"][:120]))
    return out


def _from_menumatch() -> list[tuple[str, str]]:
    path = os.path.join(EVAL_DIR, "menumatch", "items_info.txt")
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        parts = [p.strip() for p in line.split(";")]
        if len(parts) >= 4 and parts[0] and not parts[0].lower().startswith("name"):
            out.append((parts[0], f"{parts[3]} restaurant"))
    return out


def _from_fndds(limit: int) -> list[tuple[str, str]]:
    """Dish names USDA itself treats as recipes — it stores their ingredients."""
    import csv, io, zipfile
    zpath = os.path.join("data", "usda", "survey.zip")
    if not os.path.exists(zpath):
        return []
    z = zipfile.ZipFile(zpath)
    m = next(n for n in z.namelist() if n.rsplit("/", 1)[-1] == "food.csv")
    names = {r["fdc_id"]: r["description"] for r in
             csv.DictReader(io.TextIOWrapper(z.open(m), encoding="utf-8", errors="replace"))}
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    out = []
    try:
        for (fdc,) in c.execute("SELECT fdc_id FROM usda_composition"):
            d = names.get(fdc)
            # Multi-word composite names only; "Chickpeas, from canned" is not a
            # recipe anyone needs help with.
            if d and d.count(",") >= 1 and len(d.split()) >= 4:
                out.append((d, "FNDDS composite"))
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    choices=["all", "captures", "menumatch", "fndds"])
    ap.add_argument("--limit", type=int, default=40, help="dishes per source")
    args = ap.parse_args()

    from app.database import init_db
    init_db()
    from app.services.food_lookup import strong_db_match, search_foods

    sources = {}
    if args.source in ("all", "captures"):
        sources["captures"] = _from_captures()[:args.limit]
    if args.source in ("all", "menumatch"):
        sources["menumatch"] = _from_menumatch()[:args.limit]
    if args.source in ("all", "fndds"):
        sources["fndds"] = _from_fndds(args.limit)

    rows, uid = [], 1
    for src, items in sources.items():
        if not items:
            print(f"{src}: no items available")
            continue
        found = 0
        print(f"\n=== {src}: {len(items)} dishes")
        for name, context in items:
            try:
                match = await strong_db_match(name, uid)
                nearest = await search_foods(name, uid, limit=3)
            except Exception as e:
                print(f"  {name[:40]:42s} lookup failed ({type(e).__name__})")
                continue
            if match:
                found += 1
                continue
            near = [f"{f['name']} [{f['source']}]" for f in nearest[:2]]
            rows.append({"source": src, "dish": name, "context": context,
                         "nearest": near})
            print(f"  NO ROW IS THIS  {name[:38]:40s} nearest: {'; '.join(near)[:60]}")
        print(f"  -> {found}/{len(items)} had a row that IS the dish; "
              f"{len(items) - found} are recipe candidates")

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"\nwrote {len(rows)} candidates to {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
