import re
from typing import Optional

_WORD_TO_NUM: dict[str, float] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "half": 0.5, "quarter": 0.25,
}

_UNIT_TO_G: dict[str, float] = {
    "g": 1, "gram": 1, "grams": 1,
    "kg": 1000, "kilogram": 1000, "kilograms": 1000,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "pound": 453.6, "pounds": 453.6,
    "cup": 240, "cups": 240,
    "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
    "tsp": 5, "teaspoon": 5, "teaspoons": 5,
    "ml": 1, "milliliter": 1, "milliliters": 1,
    "slice": 30, "slices": 30,
    "piece": 50, "pieces": 50,
    "scoop": 35, "scoops": 35,
    "serving": 100, "servings": 100,
}

_STRIP_PREFIX = re.compile(
    r"^i\s+(?:ate|had|drank|drink|eat|have|consumed|just had|just ate)\s+",
    re.IGNORECASE,
)

_GRAMS_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(?:g|grams?)\s+(?:of\s+)?(.+)$", re.IGNORECASE
)
_VOLUME_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s+(ml|milliliters?|oz|ounces?|lb|pounds?|kg|kilograms?)\s+(?:of\s+)?(.+)$",
    re.IGNORECASE,
)
_MEASURE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s+(cups?|tbsp|tablespoons?|tsp|teaspoons?|scoops?|slices?|pieces?|servings?)\s+(?:of\s+)?(.+)$",
    re.IGNORECASE,
)
# "a bowl of stew", "two glasses of milk" — a serving-container of a food. The
# container is treated as one serving (the food's own serving size applies later).
_CONTAINER_WORDS = (
    r"bowls?|cups?|glass|glasses|plates?|cans?|bottles?|mugs?|handfuls?|pieces?|"
    r"slices?|servings?|scoops?|jars?|packets?|bars?|sticks?|wedges?|cartons?|boxes?"
)
_COUNT_TOKEN = r"a|an|one|two|three|four|five|six|seven|eight|nine|ten|half|quarter|\d+(?:\.\d+)?"
_CONTAINER_RE = re.compile(
    rf"^({_COUNT_TOKEN})\s+(?:{_CONTAINER_WORDS})\s+of\s+(.+)$", re.IGNORECASE
)
_COUNT_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(?:of\s+)?(.+)$")
_COUNT_WORD_RE = re.compile(
    r"^(a|an|one|two|three|four|five|six|seven|eight|nine|ten|half|quarter)\s+(?:of\s+)?(.+)$",
    re.IGNORECASE,
)


def _count_value(token: str) -> float:
    t = token.lower()
    if t in _WORD_TO_NUM:
        return _WORD_TO_NUM[t]
    try:
        return float(t)
    except ValueError:
        return 1.0


def parse_local(transcript: str) -> list[dict]:
    """
    Returns [{name, est_quantity_g, unit, confidence}].
    confidence 0.0–1.0: 0.9 = explicit grams, 0.5 = count, 0.3 = bare name.
    """
    text = _STRIP_PREFIX.sub("", transcript.strip())
    # Split on " and " for multiple items
    parts = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
    results = [r for p in parts if p.strip() if (r := _parse_single(p.strip()))]
    return results


def _parse_single(text: str) -> Optional[dict]:
    # "100g chicken breast" / "200 grams of rice"
    m = _GRAMS_RE.match(text)
    if m:
        return _item(m.group(2), float(m.group(1)), "g", 0.9)

    # "100ml milk" / "8oz steak"
    m = _VOLUME_RE.match(text)
    if m:
        unit = m.group(2).lower().rstrip("s")
        qty_g = float(m.group(1)) * _UNIT_TO_G.get(unit, 1)
        return _item(m.group(3), qty_g, unit, 0.85)

    # "2 cups oats" / "1 tbsp peanut butter"
    m = _MEASURE_RE.match(text)
    if m:
        unit = m.group(2).lower().rstrip("s")
        qty_g = float(m.group(1)) * _UNIT_TO_G.get(unit, 50)
        return _item(m.group(3), qty_g, unit, 0.75)

    # "a bowl of stew" / "two glasses of milk" → 1 serving of the food after "of"
    m = _CONTAINER_RE.match(text)
    if m:
        count = _count_value(m.group(1))
        return _item(m.group(2).strip(), count * 100, "serving", 0.5, servings=count)

    # "3 eggs"
    m = _COUNT_NUM_RE.match(text)
    if m:
        count = float(m.group(1))
        return _item(m.group(2).strip(), count * 100, "count", 0.5, servings=count)

    # "one apple" / "two eggs" / "a banana" / "half a dr pepper"
    m = _COUNT_WORD_RE.match(text)
    if m:
        count = _WORD_TO_NUM.get(m.group(1).lower(), 1)
        return _item(m.group(2).strip(), count * 100, "count", 0.5, servings=count)

    # bare name — assume one serving's worth, no explicit quantity
    return _item(text, 100, "assumed_100g", 0.3, servings=1)


# Time/filler words that add noise to a food name (stripped before lookup).
_FILLER_RE = re.compile(
    r"\b(?:today|tonight|yesterday|this (?:morning|afternoon|evening)|"
    r"earlier|just now|please|for (?:breakfast|lunch|dinner|a snack|snack))\b",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[.,!?;:]")
_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)

# "<food> from/at <brand>" — pull out a named brand/restaurant. The negative
# lookahead avoids "chicken from the grill" / "eggs from home".
_BRAND_SPLIT_RE = re.compile(
    r"^(.*\S)\s+(?:from|at)\s+(?!the\b|a\b|an\b|my\b|home\b|work\b|scratch\b)(\S.*)$",
    re.IGNORECASE,
)


def _split_brand(name: str):
    m = _BRAND_SPLIT_RE.match(name or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return name, None


def _clean_name(name: str) -> str:
    name = _FILLER_RE.sub(" ", name)
    name = _PUNCT_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = _ARTICLE_RE.sub("", name)   # "half a dr pepper" leaves "a dr pepper"
    return name.lower()


def _item(name: str, qty_g: float, unit: str, confidence: float, servings=None) -> dict:
    name, brand = _split_brand(name)
    return {
        "name": _clean_name(name),
        "brand": _clean_name(brand) if brand else None,   # "from chipotle" → "chipotle"
        "est_quantity_g": round(qty_g, 1),
        "est_servings": servings,   # count of servings/items when expressed that way, else None
        "unit": unit,
        "confidence": confidence,
    }
