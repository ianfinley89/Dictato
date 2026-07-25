"""Portion ladder — deterministic grams resolution.

Principle (same as create_food's per-serving fix): the model reports what it
OBSERVED — a stated weight, a count, a household measure — and THIS code does the
mass math. The model never converts household measures to grams itself; that
guessing is where both portion biases came from (restaurant sides overshot 2-4x,
cooked dishes undershot — see the Menu-Match / Nutrition5k evals).

Ladder (first rung that resolves wins; confidence reflects the rung):
  1. stated/label — an explicit weight the user said or the model read off a
     package. Trusted as-is.                                     -> high
  2. count       — servings x the food's serving_g.              -> high
  3. household   — quantity x a food-specific USDA foodPortions gram weight
     (foods.portions_json). Universal weight units (oz, lb, kg) convert
     directly; bare metric volume (ml, l, fl oz) falls back to ~1 g/ml,
     which is right for the drinks it's used on.                 -> medium
  4. estimate    — the model's gram guess, flagged.              -> low

A deterministic guard then clamps physically implausible masses — the sibling of
nutrition_guard: no model output becomes a log row without passing through here.
"""
import json
import re

# Weight units convert to grams with no food data at all.
_WEIGHT_G = {
    "g": 1.0, "gram": 1.0, "oz": 28.35, "ounce": 28.35,
    "lb": 453.6, "pound": 453.6, "kg": 1000.0,
}
# Bare volume -> grams at water density; only used when the food has no matching
# household portion, which in practice means beverages ("12 fl oz", "500 ml").
_VOLUME_ML = {
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "fl oz": 29.57, "floz": 29.57, "fluid ounce": 29.57,
}
_SYNONYMS = {
    "cups": "cup", "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsps": "tsp",
    "slices": "slice", "pieces": "piece", "links": "link", "patties": "patty",
    "grams": "g", "ounces": "oz", "ozs": "oz", "pounds": "lb", "lbs": "lb",
    "fluid ounces": "fl oz", "fl. oz": "fl oz", "fl. oz.": "fl oz",
    "milliliters": "ml", "millilitres": "ml", "liters": "l", "litres": "l",
}

_MAX_ENTRY_G = 2500.0          # nothing a person eats in one sitting weighs more
_MAX_SERVING_MULT = 20.0       # >20 servings of one food in one log is a misfire

_DESC_RE = re.compile(r"^\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s+(.+)$")

# Explicit quantity words. Bare "a"/"an" is deliberately EXCLUDED: "an order of
# jasmine rice" names a container, not a count of the food, and treating it as a
# stated count is exactly what handed unearned confidence to blind guesses
# (see scripts/calibrate_confidence.py).
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight",
                 "nine", "ten", "eleven", "twelve", "dozen", "couple", "several",
                 "half", "quarter", "third"}
_DIGIT_RE = re.compile(r"\d")


def stated_number(words: str | None) -> bool:
    """Did the user actually SAY a quantity (digit or explicit number word)?"""
    if not words:
        return False
    if _DIGIT_RE.search(words):
        return True
    return bool(set(re.findall(r"[a-z]+", words.lower())) & _NUMBER_WORDS)


def verify_claims(inp: dict, method: str, words: str | None) -> dict:
    """Strip unearned confidence from the model's own basis claim — the model is
    an unreliable narrator about the EVIDENCE it had, even when its grams are
    reasonable. A package label can only be READ in a photo; a 'stated' weight
    needs a number in the user's words; a 'count' is only trustworthy when the
    user actually counted (else it is the model inventing "1 order"). Downgrades
    never change the resolved grams — only how much the system trusts them."""
    basis = (inp.get("basis") or "").strip().lower()
    out = dict(inp)
    has_num = stated_number(words)
    if basis == "label" and method != "photo":
        out["basis"] = "estimate"
    elif basis == "stated" and not has_num:
        out["basis"] = "estimate"
    elif basis == "count":
        # Keep the count MATH (servings x serving_g beats a raw guess) but tell
        # the truth about confidence.
        out["count_verified"] = has_num
    return out


def _norm_unit(unit: str) -> str:
    u = (unit or "").strip().lower().rstrip(".")
    u = u.split(",")[0].strip()            # "cup, diced" -> "cup" (coarse is fine)
    u = _SYNONYMS.get(u, u)
    return u


def _frac(text: str) -> float:
    """'1', '0.5', '1/2', '1 1/2' -> float."""
    total = 0.0
    for part in text.split():
        if "/" in part:
            num, den = part.split("/", 1)
            total += float(num) / float(den)
        else:
            total += float(part)
    return total


def parse_usda_portions(detail: dict) -> list[dict]:
    """USDA /food/{fdcId} detail -> normalized [{unit, qty, grams, desc}].
    Handles FNDDS (portionDescription '1 cup') and SR Legacy/Foundation
    (amount + modifier/measureUnit) shapes. Keeps the first portion per unit
    (sequence order = the most typical measure)."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in detail.get("foodPortions") or []:
        grams = p.get("gramWeight")
        if not isinstance(grams, (int, float)) or grams <= 0:
            continue
        desc = (p.get("portionDescription") or "").strip()
        qty, unit = None, ""
        if desc and desc.lower() != "quantity not specified":
            m = _DESC_RE.match(desc)
            if m:
                try:
                    qty, unit = _frac(m.group(1)), m.group(2)
                except (ValueError, ZeroDivisionError):
                    continue
        else:
            qty = p.get("amount")
            unit = (p.get("modifier") or "").strip()
            mu = ((p.get("measureUnit") or {}).get("name") or "").strip()
            if not unit and mu and mu.lower() != "undetermined":
                unit = mu
        u = _norm_unit(unit)
        if not u or not isinstance(qty, (int, float)) or qty <= 0 or u in seen:
            continue
        seen.add(u)
        out.append({"unit": u, "qty": round(float(qty), 3),
                    "grams": round(float(grams), 2),
                    "desc": desc or f"{qty:g} {u}"})
    return out


# Volume and weight measures. You can eat "1 large" egg, but "one cup" is not a
# countable item — counting eggs off a cup weight gives 2 x 220 g instead of
# 2 x 61 g.
_MEASURE_UNITS = {
    "cup", "cups", "tbsp", "tsp", "tablespoon", "teaspoon", "fl oz", "floz",
    "fluid ounce", "oz", "ounce", "g", "gram", "kg", "ml", "l", "liter", "litre",
    "lb", "pound", "pint", "quart", "gallon", "cubic inch", "serving",
}


def primary_unit_grams(food: dict) -> float | None:
    """Weight of ONE of this food — "1 large" egg = 61 g — for the many generic
    USDA rows that carry no serving size. Without it "two eggs" cannot resolve as
    a count at all and collapses into a bare gram guess.

    Only COUNTABLE units qualify. If the food is only described in cups and
    spoons then "one" of it has no meaning, and returning None correctly sends
    the portion down to a flagged estimate instead of inventing a unit."""
    best = None
    for p in food.get("portions") or []:
        desc, unit = p.get("desc") or "", (p.get("unit") or "").strip().lower()
        if not p.get("grams") or not p.get("qty"):
            continue
        if _BULK_DESC_RE.search(desc) or _BULK_DESC_RE.search(unit):
            continue                    # a pot of coffee is not one coffee
        if unit in _MEASURE_UNITS:
            continue                    # a cup of egg is not an egg
        per = p["grams"] / p["qty"]
        if per <= 0 or per > _MAX_OPTION_G:
            continue
        if best is None or per > best:
            best = per
    return best


_PLURALISABLE = {
    "cup", "slice", "spear", "piece", "can", "bottle", "container", "egg",
    "tablespoon", "teaspoon", "tbsp", "tsp", "link", "patty", "cake", "cookie",
    "stick", "wedge", "fillet", "scoop", "bar", "muffin", "roll", "taco",
}


def _fmt_count(n: float) -> str:
    whole, frac = int(n), round(n - int(n), 2)
    sym = {0.25: "¼", 0.5: "½", 0.75: "¾"}.get(frac)
    if sym:
        return f"{whole}{sym}" if whole else sym
    return f"{n:g}"


def portion_label(quantity_g: float, serving_g=None, serving_desc=None,
                  portions_json=None) -> str | None:
    """Household phrasing for foods with NO serving size — "2 large", "1½ cups".
    Returns None when serving_g exists, because the client already renders that
    ("≈ 5 cakes"). This is what stops a two-egg omelette reading as bare grams."""
    if serving_g or not portions_json or not quantity_g:
        return None
    try:
        portions = (json.loads(portions_json) if isinstance(portions_json, str)
                    else portions_json) or []
    except (ValueError, TypeError):
        return None

    best = None
    for p in portions:
        desc, unit = p.get("desc") or "", p.get("unit") or ""
        if not p.get("grams") or not p.get("qty") or _BULK_DESC_RE.search(desc) \
                or _BULK_DESC_RE.search(unit):
            continue
        per = p["grams"] / p["qty"]
        if per <= 0:
            continue
        count = quantity_g / per
        if not (0.25 <= count <= 12):
            continue
        # Prefer a count that lands on a natural fraction ("2 large", not
        # "1.64 large"), then the biggest unit, so eggs beat tablespoons.
        rounded = round(count * 4) / 4
        cleanliness = abs(count - rounded) / max(count, 0.01)
        cand = (cleanliness <= 0.06, per, rounded, unit)
        if best is None or (cand[0], cand[1]) > (best[0], best[1]):
            best = cand
    if not best or not best[0]:
        return None
    _, _, rounded, unit = best
    # Only pluralise words that take an "s" — USDA units include bare adjectives
    # ("1 large" for an egg), and "2 larges" reads like a typo.
    plural = "s" if (rounded > 1 and unit in _PLURALISABLE and not unit.endswith("s")) else ""
    return f"{_fmt_count(rounded)} {unit}{plural}"


def match_household(portions: list[dict] | None, qty: float, unit: str) -> float | None:
    """qty x the food's gram weight for a matching household unit, else None."""
    u = _norm_unit(unit)
    for p in portions or []:
        if p.get("unit") == u and p.get("qty") and p.get("grams"):
            return qty * (p["grams"] / p["qty"])
    return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def resolve_grams(food: dict, inp: dict) -> dict:
    """Walk the ladder. Returns {grams, basis, confidence, note}; grams<=0 means
    nothing resolved (caller should error back to the model)."""
    qty_g = _num(inp.get("quantity_g"))
    basis = (inp.get("basis") or "").strip().lower()
    servings = _num(inp.get("servings"))
    h_qty = _num(inp.get("household_qty"))
    h_unit = (inp.get("household_unit") or "").strip()

    # Rung 1: an explicit weight — the user said it or it's printed on the package.
    if basis in ("stated", "label") and qty_g > 0:
        return {"grams": qty_g, "basis": basis, "confidence": "high", "note": None}

    # Rung 2: count x the weight of one unit. Prefer the food's own serving size;
    # failing that (most generic USDA rows have none) use one natural household
    # unit, so "two eggs" resolves as 2 x 61 g instead of collapsing to a guess.
    # The MATH is trusted either way; the CONFIDENCE depends on whether the user
    # really counted — an unverified count measured no better than a blind guess
    # (32.7% vs 34.1% median error over 123 Menu-Match items).
    if servings > 0:
        per_unit = food.get("serving_g") or primary_unit_grams(food)
        if per_unit:
            verified = bool(inp.get("count_verified"))
            return {"grams": servings * per_unit, "basis": "count",
                    "confidence": "high" if verified else "low",
                    "note": f"{servings:g} x {per_unit:g}g"
                            + ("" if verified else " (count inferred, not stated)")}

    # Rung 3: household measure.
    if h_qty > 0 and h_unit:
        u = _norm_unit(h_unit)
        if u in _WEIGHT_G:      # "4 oz", "half a pound" — pure unit math
            return {"grams": h_qty * _WEIGHT_G[u], "basis": "household",
                    "confidence": "high", "note": f"{h_qty:g} {u}"}
        m = match_household(food.get("portions"), h_qty, u)
        if m is not None:
            return {"grams": m, "basis": "household", "confidence": "medium",
                    "note": f"{h_qty:g} {u} via USDA portion weight"}
        if u in _VOLUME_ML:     # beverages: volume ~ grams
            return {"grams": h_qty * _VOLUME_ML[u], "basis": "household",
                    "confidence": "medium", "note": f"{h_qty:g} {u} at ~1g/ml"}
        # Unknown unit with no food portion data — fall through to the estimate.

    # Rung 4: the model's guess.
    if qty_g > 0:
        return {"grams": qty_g, "basis": "estimate", "confidence": "low", "note": None}
    return {"grams": 0.0, "basis": "none", "confidence": "low", "note": None}


_MAX_OPTIONS = 7
_MAX_OPTION_G = 1000.0      # a picker offers plausible single portions; Adjust
                            # still allows anything up to the guard's ceiling

# USDA mixes bulk containers and recipe yields in with real portions — "1 large
# pot (60 FO, 12 servings)" is not a serving of coffee, and offering it invites a
# catastrophic mistap. Note the plural "servings": "1 single serving container"
# IS a portion and must survive.
_BULK_DESC_RE = re.compile(
    r"\bservings\b|\b\d+\s*servings?\b|\bpot\b|\bpackage\b|\bcarton\b|\brecipe\b"
    r"|\byields?\b|\bbulk\b|\bcase\b", re.I)


def build_options(food: dict, current_g: float, prior: dict | None = None) -> list[dict]:
    """Human-meaningful portion choices for one logged entry, every gram figure
    coming from USDA data, the food's own serving size, or the user's own
    history — never from a model. This is what lets a wrong portion be fixed by
    tapping "1 can or bottle (12 fl oz)" instead of arguing about grams.

    Each option carries the ladder fields, so choosing one is recorded as the
    evidence it actually is (household / count / history) rather than a guess."""
    opts: list[dict] = []

    def add(label: str, grams: float, basis: str, **extra) -> None:
        grams = round(float(grams), 1)
        if grams <= 0 or grams > _MAX_OPTION_G:
            return
        if any(abs(o["grams"] - grams) < 0.5 for o in opts):
            return                      # same portion by another name
        opts.append({"label": label, "grams": grams, "basis": basis, **extra})

    # 1. Real USDA household measures — the best options, e.g. "1 spear" = 30 g.
    primary = None
    for p in (food.get("portions") or [])[:6]:
        if p.get("grams") and p.get("qty"):
            desc = p.get("desc") or f"{p['qty']:g} {p['unit']}"
            if _BULK_DESC_RE.search(desc) or _BULK_DESC_RE.search(p.get("unit") or ""):
                continue                # a pot of coffee is not a portion of coffee
            add(desc, p["grams"], "household",
                household_qty=p["qty"], household_unit=p["unit"])
            # The heaviest SURVIVING measure is the one people plausibly eat a
            # portion of ("1 can", not "1 fl oz"), so scale halves/doubles off it.
            if p["grams"] <= _MAX_OPTION_G and (not primary or p["grams"] > primary["grams"]):
                primary = p
    if primary:
        unit, qty, g = primary["unit"], primary["qty"], primary["grams"]
        add(f"half a {unit}" if qty == 1 else f"half ({primary['desc']})", g * 0.5,
            "household", household_qty=qty * 0.5, household_unit=unit)
        add(f"2 {unit}s" if qty == 1 else f"2 x ({primary['desc']})", g * 2,
            "household", household_qty=qty * 2, household_unit=unit)

    # 2. The food's own serving, and the two multiples people actually eat.
    sg = food.get("serving_g")
    if sg:
        one = (food.get("serving_desc") or "1 serving").strip()
        add(one if one[0].isdigit() else f"1 {one}", sg, "count", servings=1)
        add(f"half ({one})", sg * 0.5, "count", servings=0.5)
        add(f"2 x {one}", sg * 2, "count", servings=2)

    # 3. What this user normally has.
    if prior and prior.get("grams"):
        add("your usual", prior["grams"], "history")

    # 4. Only when the food knows nothing about itself (no USDA measure, no
    #    serving size — 60% of what users log) fall back to scaling the guess.
    if not opts and current_g > 0:
        add("half of this", current_g * 0.5, "estimate")
        add("this amount", current_g, "estimate")
        add("double this", current_g * 2, "estimate")

    opts.sort(key=lambda o: o["grams"])
    for o in opts:
        o["current"] = abs(o["grams"] - round(current_g, 1)) < 0.5
    return opts[:_MAX_OPTIONS]


def apply_personal_prior(res: dict, prior: dict | None) -> dict:
    """Rung 3.5: replace a BLIND guess with what this user actually eats.

    Only ever displaces basis 'estimate' — a stated weight, a real count or a
    household measure is evidence about THIS meal and always outranks a habit.
    The prior itself is gated in portion_history.personal_prior (enough kept
    logs, low variance), so reaching here means the food is genuinely habitual."""
    if not prior or res.get("basis") != "estimate" or not prior.get("grams"):
        return res
    return {"grams": float(prior["grams"]), "basis": "history",
            "confidence": "medium",
            "note": f"your usual portion for this food "
                    f"({prior['n']} {prior['kind']} log{'s' if prior['n'] != 1 else ''})"}


_SNAP_MULT = 2.0


def snap_estimate(food: dict, grams: float) -> tuple[float, str | None]:
    """Down-only cap for BLIND estimates: when the model is guessing (basis
    'estimate'), no guess may exceed 2x the food's own portion anchor —
    the largest single household portion weight when USDA has them (generic
    FNDDS/SR rows), else the row's serving_g (branded rows have no
    foodPortions but DO know their serving size; a 680g 'cheese pizza' guess
    should not sail past a row that says a serving is 140g). Never touches
    stated/count/household resolutions and never raises a low guess — the
    tool-result note tells the model it was capped, so if it truly knows more
    it can re-log with count/stated."""
    per_unit = [p["grams"] / p["qty"] for p in food.get("portions") or []
                if p.get("qty") and p.get("grams")]
    if per_unit:
        anchor, kind = max(per_unit), "largest household portion"
    elif food.get("serving_g"):
        anchor, kind = food["serving_g"], "serving"
    else:
        return grams, None
    cap = _SNAP_MULT * anchor
    if grams > cap:
        return cap, f"estimate capped at {_SNAP_MULT:g}x the {kind} ({cap:.0f}g)"
    return grams, None


def guard_grams(food: dict, grams: float) -> tuple[float, str | None]:
    """Deterministic plausibility clamp on the resolved mass."""
    note = None
    serving_g = food.get("serving_g")
    if serving_g and grams > _MAX_SERVING_MULT * serving_g:
        grams = _MAX_SERVING_MULT * serving_g
        note = f"capped at {_MAX_SERVING_MULT:g} servings"
    if grams > _MAX_ENTRY_G:
        grams = _MAX_ENTRY_G
        note = f"capped at {_MAX_ENTRY_G:g}g"
    if grams < 1.0:
        grams = 1.0
    return grams, note
