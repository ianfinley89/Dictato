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

    # Rung 2: count x the food's known serving weight.
    if servings > 0 and food.get("serving_g"):
        return {"grams": servings * food["serving_g"], "basis": "count",
                "confidence": "high", "note": f"{servings:g} x {food['serving_g']:g}g serving"}

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
