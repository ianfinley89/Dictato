"""Sanity-check AI-invented nutrition against foods of the same kind.

When a food isn't in any database the model supplies the numbers, and nothing
downstream knows whether 11 g of protein per 100 g is reasonable for a soup. It
isn't — across 35 soups in our own cache the median is 3.4 g and the 90th
percentile 13.3 — while for jerky the median is 35 g. The class is what makes a
number plausible, and we already hold enough real USDA/OFF rows to measure it
instead of guessing.

Deliberately WARNS rather than rewrites. A protein-fortified soup can genuinely
sit high, and silently "correcting" it would be quiet corruption of the user's
diary. The one exception is a mechanical unit mix-up: if reading the numbers as
per-SERVING instead of per-100 g lands them squarely in the class band, that is
near-proof of the error (the same reasoning the kJ detector uses), and the model
is told to resubmit with values_per='serving' rather than having its numbers
edited behind its back.

Neighbours come only from usda/off — genuine per-100 g sources. FatSecret encodes
per-serving values, and web/estimate rows are the very thing being checked, so
including either would let bad data validate more bad data.
"""
import json
import statistics

from app.database import get_conn

_TRUSTED = ("usda", "off")
_MIN_NEIGHBOURS = 8      # below this the "class" is noise, not a distribution
_TOO_HIGH = 2.0          # multiple of the class 90th percentile that reads wrong
_FIELDS = ("calories", "protein_g", "carbs_g", "fat_g")

# Words that never identify a food class.
_SKIP = {
    "the", "and", "with", "from", "made", "style", "flavor", "flavour", "fresh",
    "organic", "natural", "original", "classic", "homemade", "restaurant",
    "large", "small", "medium", "mix", "brand", "protein", "free", "low",
}


def _class_tokens(name: str) -> list[str]:
    """Candidate class words, most specific first.

    English food names are head-final — "vietnamese chicken glass noodle SOUP",
    "kodiak protein PANCAKES" — so the last word usually names the class."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in (name or "").lower()).split()
             if len(w) > 2 and w not in _SKIP]
    return list(reversed(words))


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def neighbour_bands(name: str, min_n: int = _MIN_NEIGHBOURS) -> dict | None:
    """Per-100 g distribution for foods of the same class, or None if we don't
    hold enough of them to say anything honest."""
    for token in _class_tokens(name):
        rows = _neighbour_rows(token)
        if len(rows) < min_n:
            continue
        bands = {"class": token, "n": len(rows)}
        for field in _FIELDS:
            vals = [r[field] for r in rows if r.get(field) is not None]
            if not vals:
                continue
            bands[field] = {"median": round(statistics.median(vals), 2),
                            "p90": round(_percentile(vals, 0.9), 2),
                            "max": round(max(vals), 2)}
        return bands
    return None


def _neighbour_rows(token: str) -> list[dict]:
    marks = ",".join("?" * len(_TRUSTED))
    try:
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT f.nutrients_json FROM foods_fts
                    JOIN foods f ON f.id = foods_fts.rowid
                    WHERE foods_fts MATCH ? AND f.source IN ({marks})
                    LIMIT 300""",
                (f'"{token}"', *_TRUSTED),
            ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            n = json.loads(r["nutrients_json"])
        except (ValueError, TypeError):
            continue
        if (n.get("calories") or 0) > 0:     # rows with no energy tell us nothing
            out.append(n)
    return out


def _offenders(nutrients: dict, bands: dict) -> list[dict]:
    found = []
    for field in _FIELDS:
        band = bands.get(field)
        value = nutrients.get(field)
        if not band or value is None:
            continue
        # Three ceilings, most permissive wins. `max * 1.5` means the value has
        # to beat everything ever seen by half again; `median * 3` keeps a class
        # whose samples happen to be near-identical from flagging ordinary food
        # (10 soups all listing 1.5 g of fat would otherwise make 3.5 g look
        # extreme). Only a genuine outlier clears all three.
        ceiling = max(band["p90"] * _TOO_HIGH, band["max"] * 1.5, band["median"] * 3.0)
        if value > ceiling and value > 1.0:
            found.append({"field": field, "value": round(value, 1),
                          "class_median": band["median"], "class_max": band["max"]})
    return found


def check_nutrition(name: str, nutrients: dict, serving_g: float | None = None) -> dict | None:
    """None when the numbers look normal for this kind of food. Otherwise
    {message, offenders, class, n, likely_per_serving}."""
    bands = neighbour_bands(name)
    if not bands:
        return None                      # no comparable foods — say nothing
    offenders = _offenders(nutrients, bands)
    if not offenders:
        return None

    # Would reading these as per-serving explain it? That is the common mistake
    # and it is checkable rather than guessable.
    likely_per_serving = False
    if serving_g and serving_g > 100:
        # If these are really per-SERVING numbers filed as per-100g, the true
        # per-100g figure is value / (serving_g/100). Landing in the class band
        # after that division is near-proof of the mix-up.
        rescaled = {f: (nutrients.get(f) or 0) * 100.0 / serving_g for f in _FIELDS}
        likely_per_serving = not _offenders(rescaled, bands)

    # Name the most egregious field, not just the first one checked.
    worst = max(offenders, key=lambda o: o["value"] / max(o["class_max"], 0.1))
    msg = (f"{worst['field'].replace('_g', '')} {worst['value']} per 100g is high for "
           f"'{bands['class']}' — across {bands['n']} database foods the median is "
           f"{worst['class_median']} and the highest seen is {worst['class_max']}.")
    if likely_per_serving:
        msg += (f" These look like PER-SERVING numbers: divided by the {serving_g:g}g "
                f"serving they land in the normal range. Re-send with "
                f"values_per='serving'.")
    else:
        msg += " Double-check the label or source before trusting it."
    return {"message": msg, "offenders": offenders, "class": bands["class"],
            "n": bands["n"], "likely_per_serving": likely_per_serving}
