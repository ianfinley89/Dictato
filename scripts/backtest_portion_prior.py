"""Temporal backtest of the personal portion prior against real logged history.

WHAT THIS CAN AND CANNOT SHOW — read before quoting numbers:

  It CANNOT measure accuracy. For an accepted entry the logged grams ARE the
  model's guess, so comparing a prior against it measures agreement with the
  incumbent, not correctness (the same circularity that invalidated scoring
  models against accepted capture labels).

  It CAN measure, honestly:
    - COVERAGE     — how often the prior would have fired at all
    - INTERVENTION — how far it would have moved the logged grams
    - SAFETY       — whether it fires on context-dependent foods where a single
                     habitual number is the wrong idea (the milk case)

Walks each user's history in chronological order and, at every entry, builds the
prior from ONLY the entries that existed before it — the information the app
would really have had.

  uv run python scripts/backtest_portion_prior.py         # live DB, READ-ONLY
"""
import os
import statistics
import sys

sys.path.insert(0, ".")

LIVE_DB = os.path.join("data", "dictato.db")
_MIN_N_HABITUAL, _MIN_N_VERIFIED, _MAX_CV = 3, 1, 0.25


def _prior(pool: list[tuple[float, str]]) -> dict | None:
    """Mirror of portion_history.personal_prior over an in-memory pool."""
    verified = [q for q, s in pool if s == "manual"]
    habitual = [q for q, _ in pool]
    for vals, kind, min_n in ((verified, "verified", _MIN_N_VERIFIED),
                              (habitual, "habitual", _MIN_N_HABITUAL)):
        if len(vals) < min_n:
            continue
        mean = statistics.mean(vals)
        cv = statistics.stdev(vals) / mean if len(vals) > 1 and mean else 0.0
        if cv <= _MAX_CV and mean > 0:
            return {"grams": round(statistics.median(vals), 1), "n": len(vals),
                    "cv": round(cv, 3), "kind": kind}
    return None


def main() -> None:
    import sqlite3
    if not os.path.exists(LIVE_DB):
        sys.exit(f"no live DB at {LIVE_DB}")
    db = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT le.user_id, le.food_id, f.name, le.quantity_g, le.source, le.eaten_at
           FROM log_entries le JOIN foods f ON f.id = le.food_id
           ORDER BY le.user_id, le.food_id, le.eaten_at, le.id""").fetchall()
    db.close()

    hist: dict[tuple[int, int], list[tuple[float, str]]] = {}
    fired, skipped, deltas, examples, blocked = 0, 0, [], [], {}
    for r in rows:
        key = (r["user_id"], r["food_id"])
        pool = hist.setdefault(key, [])
        p = _prior(pool)                      # only prior knowledge, as it happened
        if p:
            fired += 1
            pct = abs(p["grams"] - r["quantity_g"]) / max(r["quantity_g"], 1) * 100
            deltas.append(pct)
            if pct > 15:
                examples.append((r["name"], r["quantity_g"], p, pct))
        else:
            skipped += 1
            if len(pool) >= _MIN_N_HABITUAL:  # enough data, blocked by the CV gate
                vals = [q for q, _ in pool]
                mean = statistics.mean(vals)
                blocked[r["name"]] = round(statistics.stdev(vals) / mean, 2) if mean else 0
        pool.append((r["quantity_g"], r["source"]))

    total = fired + skipped
    print(f"entries walked: {total}")
    print(f"prior WOULD have fired: {fired} ({fired / max(total, 1):.0%} coverage)"
          f"   — and only on basis='estimate' logs in production, so live coverage is lower")
    print(f"no prior (thin or too variable): {skipped}")
    if deltas:
        print(f"\nintervention size |prior - logged|: median {statistics.median(deltas):.1f}%"
              f"  mean {statistics.mean(deltas):.1f}%  max {max(deltas):.1f}%")
        print("(NOT an error rate — the logged value is usually the model's own guess)")
    if examples:
        print("\nwhere it would have changed the number most:")
        for name, logged, p, pct in sorted(examples, key=lambda t: -t[3])[:8]:
            print(f"  {name[:38]:38s} logged {logged:6.0f}g -> prior {p['grams']:6.0f}g "
                  f"({pct:5.0f}% move, from {p['n']} {p['kind']} logs, cv {p['cv']})")
    if blocked:
        print("\nSAFETY — foods with enough history that the CV gate correctly refused:")
        for name, cv in sorted(blocked.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {name[:38]:38s} cv {cv}  -> no prior (stays a flagged guess)")
    print("\nTo judge accuracy, watch undo/adjust rates by portion_basis in the admin"
          "\npane as real captures accumulate — behaviour is the non-circular signal.")


if __name__ == "__main__":
    main()
