"""What a typical serving of this KIND of food weighs.

Half the foods people log carry no serving size and no USDA household measure, so
a blind gram guess has nothing to bound it — which is where the 2-4x restaurant
side overshoot lives. But the database already knows what a serving of "yogurt"
or "beef" weighs, across many rows; we simply never asked.

Learned from cached USDA/Open Food Facts servings, never hard-coded, and gated on
SPREAD as well as count: yogurt is 150 g with a p25-p75 of 150-159 and is worth
trusting, while "soup" ranges 51-245 g and is not a number anyone should lean on.
A wide class returns nothing rather than a confident average — the same gate that
keeps the personal portion prior off context-dependent foods like milk.
"""
import re
import statistics

from app.database import get_conn

_TRUSTED = ("usda", "off")
_MIN_SAMPLES = 5          # fewer than this is anecdote, not a distribution
_MAX_SPREAD = 1.6         # p75/p25; above this the class has no typical size
_MIN_G, _MAX_G = 5.0, 1500.0
# A unit smaller than this is a piece, not a portion: USDA lists "1 piece" of
# dried cranberry at 0.4 g and "1 slice" of fried plantain at 5 g. Using either
# as the serving anchor would clamp a 30 g cranberry log to under a gram.
_MIN_ANCHOR_G = 15.0

# Words that describe a food without naming its kind.
_SKIP = {
    "the", "and", "with", "from", "made", "style", "fresh", "organic", "natural",
    "original", "classic", "raw", "cooked", "frozen", "prepared", "plain", "low",
    "free", "whole", "food", "foods", "mix", "flavored", "flavoured", "brand",
    "large", "small", "medium", "nfs", "ns", "form", "added", "fat", "protein",
}

_cache: dict[str, dict | None] = {}


def class_words(name: str) -> list[str]:
    """Candidate class words, most specific first. English food names are
    head-final — "grilled chicken CAESAR SALAD" — so the last word usually names
    the kind."""
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower())
             if len(w) > 2 and w not in _SKIP]
    return list(reversed(words))


def _sample(word: str) -> list[float]:
    """Servings of foods that ARE this kind of thing.

    Full-text matching alone is not enough: searching "milk" also returns milk
    chocolate and milk shakes, whose servings are nothing like a glass of milk,
    and the resulting spread makes a perfectly well-defined class look
    untrustworthy. Keep only rows where the word is the head noun."""
    marks = ",".join("?" * len(_TRUSTED))
    try:
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT f.name, f.serving_g FROM foods_fts
                    JOIN foods f ON f.id = foods_fts.rowid
                    WHERE foods_fts MATCH ? AND f.source IN ({marks})
                      AND f.serving_g > 0
                    LIMIT 600""",
                (f'"{word}"', *_TRUSTED),
            ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        heads = class_words(r["name"])
        if heads and heads[0] == word and _MIN_G <= r["serving_g"] <= _MAX_G:
            out.append(r["serving_g"])
    return out


def typical_serving(name: str) -> dict | None:
    """{grams, class, n, spread} for this kind of food, or None when the database
    has no confident answer."""
    # ONLY the most specific class word. Falling back to a broader one looks like
    # extra coverage and is actually a confident lie: "beef jerky" has too few
    # samples under "jerky", and retreating to "beef" returns 112 g — a steak
    # serving, four times a jerky pack. No answer beats a wrong one.
    words = class_words(name)[:1]
    for word in words:
        if word in _cache:
            hit = _cache[word]
            if hit:
                return hit
            continue
        vals = _sample(word)
        if len(vals) < _MIN_SAMPLES:
            _cache[word] = None
            continue
        vals.sort()
        p25, p75 = vals[len(vals) // 4], vals[3 * len(vals) // 4]
        spread = (p75 / p25) if p25 > 0 else 999.0
        if spread > _MAX_SPREAD:
            _cache[word] = None          # no typical size worth quoting
            continue
        out = {"grams": round(statistics.median(vals), 1), "class": word,
               "n": len(vals), "spread": round(spread, 2)}
        _cache[word] = out
        return out
    return None


def clear_cache() -> None:
    """Class stats are derived from the food cache, which grows — tests and
    long-running processes need a way to forget."""
    _cache.clear()


def anchor_grams(food: dict) -> tuple[float | None, str]:
    """The best "one serving" figure available for this food, and where it came
    from: its own serving size, its largest countable USDA measure, or the
    typical serving for its kind."""
    if food.get("serving_g"):
        return float(food["serving_g"]), "serving"
    from app.services.portion import primary_unit_grams      # local: avoids a cycle
    per_unit = primary_unit_grams(food)
    # primary_unit_grams answers "what is ONE of these" (right for counting three
    # cranberries); a serving anchor needs something a person actually eats.
    if per_unit and per_unit >= _MIN_ANCHOR_G:
        return per_unit, "measure"
    klass = typical_serving(food.get("name") or "")
    if klass:
        return klass["grams"], f"typical {klass['class']}"
    return None, "none"
