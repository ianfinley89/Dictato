"""Does searching a plain food name return a number you could actually eat?

The failure this measures is not "wrong food" in the abstract — it is that a
packaged product quotes its per-100g AS SOLD. For anything you cook that is the
DRY weight, and packaged goods are titled exactly how people speak ("OATMEAL",
"WHITE RICE"), so they beat "Oatmeal, NFS" on an exact short-name match. Someone
logging a bowl of oatmeal got 377 kcal/100g against a real 71.

Ground truth here is a PLAUSIBILITY BAND for the food as eaten, not a single
number — several of these foods legitimately range (a caesar salad with or
without dressing), and a band states what is actually known. Bands are wide on
purpose: this measures "is the answer edible", not "is it the best row".

Each query runs against a FRESH database. Sharing one warms the cache with
earlier queries' results and the ranking then depends on query order, which
silently flattered an earlier version of this measurement.

    uv run python scripts/eval_search_ranking.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, ".")

# kcal/100g as the food is normally EATEN, with a generous band.
CASES = [
    ("scrambled eggs",         130, 230),
    ("grilled chicken breast", 130, 230),
    ("steamed broccoli",        20,  80),
    ("white rice",              95, 185),   # cooked; dry is ~360
    ("black coffee",             0,  20),
    ("greek yogurt",            40, 150),
    ("cheddar cheese",         320, 430),
    ("ground beef",            180, 340),
    ("baked potato",            70, 140),
    ("spaghetti",              120, 210),   # cooked; dry is ~370
    ("roasted almonds",        540, 690),
    ("banana",                  75, 115),
    ("orange juice",            35,  65),
    ("whole milk",              50,  80),
    ("peanut butter",          540, 650),
    ("salmon fillet",          130, 240),
    ("french fries",           140, 340),
    ("oatmeal",                 50, 120),   # cooked; dry oats are ~380
    ("cooked chicken thigh",   150, 280),
    ("mashed potatoes",         80, 160),
]


async def main() -> None:
    passed, rows = 0, []
    for query, lo, hi in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATABASE_PATH"] = os.path.join(tmp, "eval.db")
            for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
                del sys.modules[mod]
            from app.database import init_db
            init_db()
            from app.services.food_lookup import search_foods
            try:
                res = await search_foods(query, user_id=1, limit=5)
            except Exception as e:
                rows.append((query, None, None, f"error: {type(e).__name__}"))
                continue
        if not res:
            rows.append((query, None, None, "no results"))
            continue
        top = res[0]
        kcal = float(top["nutrients_per_100g"].get("calories") or 0)
        ok = lo <= kcal <= hi
        passed += ok
        rows.append((query, kcal, ok, f"{top['name'][:38]}"
                     + (f"  [{top['brand'][:18]}]" if top.get("brand") else "")))

    print(f"{'query':24s} {'kcal':>7}  {'band':>11}  top result")
    for (query, lo, hi), (q, kcal, ok, note) in zip(CASES, rows):
        mark = "ok  " if ok else "MISS"
        k = f"{kcal:7.1f}" if kcal is not None else "      -"
        print(f"{mark} {query:22s} {k}  {lo:4d}-{hi:<4d}  {note}")
    print(f"\ntop-1 plausible: {passed}/{len(CASES)}")


if __name__ == "__main__":
    asyncio.run(main())
