"""Rewrite per-entry nutrition snapshots that were frozen from a corrupt food row.

Snapshots are computed at log time on purpose, so a past day never changes under
the user. That property is worth keeping for anything debatable — but not for a
unit-conversion bug. USDA SR Legacy returns two nutrients both named "Energy",
kcal and kJ, and the parser kept whichever arrived last, so a raw carrot cached
at 173 kcal/100g instead of 41 (see `_parse_usda`). `_repair_nutrients` fixes the
`foods` rows at startup; the entries logged against them keep the inflated
numbers until something rewrites them, which is this.

Deliberately a one-off script rather than a startup pass: rewriting history is
not something that should happen quietly on every boot.

Reports what it would change and exits; pass --apply to write.

    uv run python scripts/repair_entry_snapshots.py
    uv run python scripts/repair_entry_snapshots.py --apply
"""
import argparse
import json
import sys

sys.path.insert(0, ".")

from app.database import get_conn                      # noqa: E402
from app.services.logging import _snapshot             # noqa: E402

# Below this the difference is rounding, not corruption.
_MIN_KCAL_DIFF = 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrected snapshots")
    args = ap.parse_args()

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT e.id, e.user_id, e.quantity_g, e.eaten_at, e.nutrients_snapshot_json,
                      f.name, f.nutrients_json
               FROM log_entries e JOIN foods f ON f.id = e.food_id
               WHERE f.source IN ('usda', 'off')"""
        ).fetchall()

        stale = []
        for r in rows:
            try:
                old = json.loads(r["nutrients_snapshot_json"])
                per100 = json.loads(r["nutrients_json"])
            except (TypeError, ValueError):
                continue
            new = _snapshot(per100, r["quantity_g"])
            if abs((new.get("calories") or 0) - (old.get("calories") or 0)) >= _MIN_KCAL_DIFF:
                stale.append((r, old, new))

        print(f"{len(rows)} entries on cached food rows; {len(stale)} disagree with the "
              f"food's current nutrition\n")
        for r, old, new in stale:
            print(f"  entry {r['id']:<5} user {r['user_id']}  {r['eaten_at'][:10]}  "
                  f"{r['name'][:26]:28s} {r['quantity_g']:5.0f}g   "
                  f"{old.get('calories'):7.1f} -> {new.get('calories'):6.1f} kcal")

        if not stale:
            return
        if not args.apply:
            print("\ndry run — pass --apply to write")
            return
        for r, _old, new in stale:
            conn.execute("UPDATE log_entries SET nutrients_snapshot_json=? WHERE id=?",
                         (json.dumps(new), r["id"]))
        print(f"\nrewrote {len(stale)} snapshots")


if __name__ == "__main__":
    main()
