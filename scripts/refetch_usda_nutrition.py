"""Re-read cached USDA foods with the corrected energy parser.

`_parse_usda` used to key nutrients by name alone, and SR Legacy rows carry two
nutrients both called "Energy" — kcal and kJ — so the kJ figure won and those
foods cached at 4.184x their real calories.

`nutrition_guard` repairs the obvious cases, but it cannot repair all of them and
must not try. A high-fibre vegetable's Atwater estimate overshoots its true
energy, which drags the kJ ratio below the guard's band: raw green leaf lettuce
sits at 3.39x, under the 3.5 floor. Widening the band is not available — beer
(2.68x) and a Black Russian (3.23x) sit in the same range and are CORRECT, being
alcohol, whose energy legitimately outruns its macros. A heuristic that
separates them does not exist.

So ask USDA again. Exact, not inferred. Uses the bulk endpoint (20 ids per call),
so the whole cache costs a few dozen requests.

    uv run python scripts/refetch_usda_nutrition.py
    uv run python scripts/refetch_usda_nutrition.py --apply
"""
import argparse
import asyncio
import json
import sys

import httpx

sys.path.insert(0, ".")

from app.config import USDA_API_KEY                              # noqa: E402
from app.database import get_conn                                # noqa: E402
from app.services.food_lookup import USDA_BASE, _parse_usda      # noqa: E402
from app.services.nutrition_guard import sanitize_per_100g       # noqa: E402

_CHUNK = 20                  # the bulk endpoint's documented maximum
_MIN_DIFF = 1.0              # below this it is rounding, not corruption


def _flatten(item: dict) -> dict:
    """The detail/bulk endpoint nests each nutrient under `nutrient` and calls the
    value `amount`; the search endpoint used by `_parse_usda` puts `nutrientName`
    and `value` at the top level. Same data, two shapes — reshape rather than
    teach the production parser a dialect only this script sees.

    Without this every nutrient reads as missing and every food re-parses to 0
    kcal, which on a dry run looks exactly like "the whole cache is wrong"."""
    out = dict(item)
    out["foodNutrients"] = [
        {"nutrientName": (n.get("nutrient") or {}).get("name"),
         "unitName": (n.get("nutrient") or {}).get("unitName"),
         "value": n.get("amount")}
        if "nutrient" in n else n
        for n in item.get("foodNutrients") or []
    ]
    return out


async def _fetch(client: httpx.AsyncClient, ids: list[str]) -> list[dict]:
    r = await client.post(f"{USDA_BASE}/foods", params={"api_key": USDA_API_KEY},
                          json={"fdcIds": ids, "format": "full"})
    r.raise_for_status()
    return r.json()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrected nutrition")
    args = ap.parse_args()
    if not USDA_API_KEY:
        print("USDA_FOOD_DATA_API_KEY is not set")
        return

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_id, name, nutrients_json FROM foods
               WHERE source='usda' AND source_id IS NOT NULL"""
        ).fetchall()
    cached = {str(r["source_id"]): r for r in rows}
    ids = list(cached)
    print(f"{len(ids)} cached USDA foods; re-reading in {(len(ids) + _CHUNK - 1) // _CHUNK} calls\n")

    changed, failed = [], 0
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            try:
                items = await _fetch(client, chunk)
            except Exception as e:
                failed += len(chunk)
                print(f"  chunk {i // _CHUNK}: {type(e).__name__} — skipped")
                continue
            for item in items:
                fdc = str(item.get("fdcId") or "")
                row = cached.get(fdc)
                if not row:
                    continue
                parsed = _parse_usda(_flatten(item))
                if not parsed:
                    continue
                fresh, _ = sanitize_per_100g(json.loads(parsed["nutrients_json"]))
                old = json.loads(row["nutrients_json"])
                if abs((fresh.get("calories") or 0) - (old.get("calories") or 0)) >= _MIN_DIFF:
                    # Keep any micros we already hold; only energy/macros are suspect.
                    merged = {**old, **{k: fresh[k] for k in
                                        ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g")
                                        if k in fresh}}
                    changed.append((row, old, merged))

    print(f"\n{len(changed)} of {len(ids)} foods disagree with USDA"
          + (f", {failed} unreadable" if failed else ""))

    # Classify, because "1230 rows changed" and "1230 rows were wrong" are very
    # different claims and only one of them justifies a rewrite.
    buckets: dict[str, list] = {"kJ (~4.2x)": [], "large (>10%)": [], "small (<10%)": []}
    for row, old, new in changed:
        o, n = (old.get("calories") or 0), (new.get("calories") or 0)
        ratio = (o / n) if n else 0
        key = ("kJ (~4.2x)" if 3.9 <= ratio <= 4.5 else
               "large (>10%)" if n and abs(o - n) / n > 0.10 else "small (<10%)")
        buckets[key].append((row, o, n))
    for key, items in buckets.items():
        print(f"\n  {key}: {len(items)}")
        for row, o, n in sorted(items, key=lambda x: -abs(x[1] - x[2]))[:8]:
            print(f"      {row['name'][:40]:42s} {o:7.1f} -> {n:6.1f} kcal/100g")
    if not changed or not args.apply:
        if changed:
            print("\ndry run — pass --apply to write")
        return

    with get_conn() as conn:
        for row, _old, new in changed:
            conn.execute("UPDATE foods SET nutrients_json=? WHERE id=?",
                         (json.dumps(new), row["id"]))
    print(f"\nrewrote {len(changed)} foods — now run scripts/repair_entry_snapshots.py")


if __name__ == "__main__":
    asyncio.run(main())
