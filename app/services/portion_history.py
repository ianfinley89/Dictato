"""The user's own habitual portion — a personal prior for the portion ladder.

The research literature calls personalized priors "under-explored despite obvious
feasibility": a frontier model can only guess a generic portion, while the app
knows what THIS person actually eats. Their usual coffee, their protein shake,
their rice cake.

Why the history is trustworthy: undoing an entry DELETES its row, so
`log_entries` holds only portions the user kept, and rows with source='manual'
were typed by hand through Adjust — human-VERIFIED portions. The table curates
itself, which is what keeps this out of the circular trap of scoring a model
against its own accepted output.

Why it is gated hard: habituality is real but not universal. Live data shows a
median portion CV of 0.12 across repeated user-food pairs (people are creatures
of habit) with genuine exceptions — milk came in at CV 0.85 because 30 g is a
splash in coffee and 150 g is a glass. A prior on a food like that would be
confidently wrong, so high-variance foods get NO prior and stay flagged as
guesses.

NOTE for evals: scratch DBs persist between runs, so history accumulates across
them. Use --fresh for a clean A/B or the second run silently differs.
"""
import statistics

from app.database import get_conn

_LOOKBACK = 20          # recent habits only; ancient portions aren't "usual"
_MIN_N_HABITUAL = 3     # an inferred habit needs at least three points
_MIN_N_VERIFIED = 1     # ONE hand-typed portion outranks three accepted guesses:
                        # the user stated it. This also makes the prior
                        # self-correcting — a single Adjust immediately replaces a
                        # bad habit the app had inferred, instead of waiting for
                        # the accepted values to be outvoted.
_MAX_CV = 0.25          # above this the food is context-dependent, not habitual


def _stats(qtys: list[float]) -> tuple[float, float]:
    """(median, coefficient of variation) — median because it shrugs off one
    outlier, CV because it is scale-free across foods."""
    mean = statistics.mean(qtys)
    cv = statistics.stdev(qtys) / mean if len(qtys) > 1 and mean else 0.0
    return statistics.median(qtys), cv


def personal_prior(uid: int, food_id: int) -> dict | None:
    """This user's habitual grams for this food, or None when their history is
    too thin or too variable to be worth trusting."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT quantity_g, source, portion_manual FROM log_entries
               WHERE user_id=? AND food_id=? AND quantity_g > 0
               ORDER BY eaten_at DESC, id DESC LIMIT ?""",
            (uid, food_id, _LOOKBACK),
        ).fetchall()
    if not rows:
        return None

    # "Verified" = the user set this number themselves: a manual entry, or a
    # portion they picked/adjusted on a voice or photo log.
    verified = [r["quantity_g"] for r in rows
                if r["source"] == "manual" or r["portion_manual"]]
    habitual = [r["quantity_g"] for r in rows]
    # Hand-corrected portions win: the user told us this number directly.
    for pool, kind, min_n in ((verified, "verified", _MIN_N_VERIFIED),
                              (habitual, "habitual", _MIN_N_HABITUAL)):
        if len(pool) < min_n:
            continue
        grams, cv = _stats(pool)
        if cv <= _MAX_CV and grams > 0:
            return {"grams": round(grams, 1), "n": len(pool),
                    "cv": round(cv, 3), "kind": kind}
    return None
