"""Weigh-ins: captured PASSIVELY from what the user already says.

Weight is the one signal that can correct systematic logging bias (logged intake
vs. observed weight trend), but nagging people to enter it defeats the app's
speed-first premise. So the primary path is passive: say "I weighed 182 this
morning" into the normal voice capture — or mention it to the coach — and it's
recorded with ZERO model tokens. The Settings field is the explicit fallback.

Parsing is deliberately CONSERVATIVE: a false weigh-in silently corrupts the
trend that everything else would be calibrated against, so a match requires an
explicit weight trigger ("weigh", "scale said", "I'm N lb") and rejects goal
talk ("I want to be 180"). Weights are stored canonically in kg; the unit the
user spoke in is kept for display.
"""
import re
from datetime import datetime, timezone

from app.database import get_conn

LB_PER_KG = 2.20462
_KG_PER_LB = 1 / LB_PER_KG
_KG_PER_STONE = 6.35029

# Plausible adult range, post-conversion. Anything outside is a parse error, not
# a person.
_MIN_KG, _MAX_KG = 20.0, 350.0

# An explicit statement that this number IS the user's body weight.
_TRIGGER_RE = re.compile(
    r"\b(weigh(?:ed|s|ing|t)?|scale\s+(?:said|says|read|reads)"
    r"|stepped\s+on\s+the\s+scale)\b", re.I)
# "I'm 182 lb" — allowed without a weigh-word, but only WITH a unit, so
# "I'm 30 minutes late" can never match.
_SELF_RE = re.compile(
    r"\b(?:i'?m|i\s+am)\s+(?:at\s+|down\s+to\s+|up\s+to\s+)?"
    r"(\d{2,3}(?:\.\d+)?)\s*(lbs?|pounds?|kgs?|kilos?|kilograms?)\b", re.I)
# Wanting to weigh something is not weighing it.
_GOAL_RE = re.compile(
    r"\b(goal|target|aim(?:ing)?|want|wish|hope|hoping|plan(?:ning)?|"
    r"trying\s+to|would\s+like|need\s+to\s+(?:get|be)|by\s+summer)\b", re.I)

_NUM_UNIT_RE = re.compile(
    r"(\d{2,3}(?:\.\d+)?)\s*(lbs?|pounds?|kgs?|kilos?|kilograms?|stone|st)?\b", re.I)

_UNITS = {
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg",
    "stone": "stone", "st": "stone",
}

# Words that carry no meaning of their own when deciding "was that ONLY a
# weigh-in?" — time references and filler.
_FILLER = {
    "i", "im", "am", "my", "me", "was", "is", "at", "the", "a", "an", "this",
    "that", "today", "morning", "afternoon", "evening", "tonight", "now",
    "just", "on", "in", "and", "so", "of", "it", "up", "down", "to", "weigh",
    "weighed", "weighs", "weighing", "weight", "scale", "said", "says", "read",
    "reads", "stepped", "lb", "lbs", "pound", "pounds", "kg", "kgs", "kilo",
    "kilos", "kilogram", "kilograms", "stone", "st", "myself", "again",
    "yesterday", "currently", "still", "about", "around", "roughly",
}


def _to_kg(value: float, unit: str) -> float:
    if unit == "kg":
        return value
    if unit == "stone":
        return value * _KG_PER_STONE
    return value * _KG_PER_LB


def _infer_unit(value: float, last_kg: float | None) -> str:
    """No unit spoken. Prefer whichever reading sits closest to the user's own
    history; with no history, fall back to magnitude (US-centric default)."""
    if last_kg:
        as_lb, as_kg = _to_kg(value, "lb"), value
        return "lb" if abs(as_lb - last_kg) <= abs(as_kg - last_kg) else "kg"
    return "lb" if value >= 140 else ("kg" if value <= 90 else "lb")


def parse_weight(text: str | None, last_kg: float | None = None) -> dict | None:
    """Deterministically pull a body weight out of ordinary speech.
    Returns {weight_kg, unit, value, display, span} or None."""
    if not text:
        return None
    if _GOAL_RE.search(text):
        return None

    m_self = _SELF_RE.search(text)
    if m_self:
        value, unit = float(m_self.group(1)), _UNITS[m_self.group(2).lower()]
        span = m_self.span()
    else:
        trig = _TRIGGER_RE.search(text)
        if not trig:
            return None
        # Take the first number at or after the trigger; fall back to one before
        # it ("182 is what the scale said").
        after = [m for m in _NUM_UNIT_RE.finditer(text) if m.start() >= trig.start()]
        before = [m for m in _NUM_UNIT_RE.finditer(text) if m.end() <= trig.start()]
        m = (after or before[-1:] or [None])[0]
        if not m:
            return None
        value = float(m.group(1))
        raw_unit = (m.group(2) or "").lower()
        unit = _UNITS.get(raw_unit) or _infer_unit(value, last_kg)
        span = (min(trig.start(), m.start()), max(trig.end(), m.end()))

    kg = _to_kg(value, unit)
    if not (_MIN_KG <= kg <= _MAX_KG):
        return None
    disp_unit = "lb" if unit == "lb" else ("kg" if unit == "kg" else "st")
    return {"weight_kg": round(kg, 2), "unit": "kg" if unit == "kg" else "lb",
            "value": value, "display": f"{value:g} {disp_unit}", "span": span}


def is_weight_only(text: str, span: tuple[int, int]) -> bool:
    """True when the capture said nothing but the weigh-in — then there is no
    food to log and the model never needs to run (zero tokens)."""
    leftover = (text[:span[0]] + " " + text[span[1]:]).lower()
    words = [w for w in re.findall(r"[a-z']+", leftover) if w.replace("'", "") not in _FILLER]
    return len(words) == 0


def record_weight(uid: int, weight_kg: float, unit: str = "lb",
                  source: str = "manual", measured_at: str | None = None) -> dict:
    """Store a weigh-in (one per user/day/source — a re-statement corrects it)."""
    weight_kg = max(_MIN_KG, min(_MAX_KG, float(weight_kg)))
    when = measured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    day = when[:10]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM weigh_ins WHERE user_id=? AND DATE(measured_at)=? AND source=?",
            (uid, day, source))
        cur = conn.execute(
            """INSERT INTO weigh_ins (user_id, weight_kg, unit, measured_at, source)
               VALUES (?,?,?,?,?)""",
            (uid, round(weight_kg, 2), "kg" if unit == "kg" else "lb", when, source))
        wid = cur.lastrowid
    return {"id": wid, "weight_kg": round(weight_kg, 2), "unit": unit,
            "measured_at": when, "source": source}


def latest_weight(uid: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, weight_kg, unit, measured_at, source FROM weigh_ins
               WHERE user_id=? ORDER BY measured_at DESC, id DESC LIMIT 1""",
            (uid,)).fetchone()
    return dict(row) if row else None


def recent_weights(uid: int, days: int = 90, limit: int = 60) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, weight_kg, unit, measured_at, source FROM weigh_ins
               WHERE user_id=? AND measured_at >= datetime('now', ?)
               ORDER BY measured_at DESC, id DESC LIMIT ?""",
            (uid, f"-{int(days)} days", limit)).fetchall()
    return [dict(r) for r in rows]


# Keys the coach/logging model plausibly invents when it remembers a weigh-in.
_FACT_KEYS = ("weight_lb", "weight_lbs", "weight_kg", "weight", "body_weight",
              "weigh_in", "weigh_ins", "weighins", "current_weight")


def record_from_facts(uid: int, facts: dict) -> int:
    """Mirror weight facts the model remembered into the canonical table, so the
    profile stays the model's scratchpad and weigh_ins stays the source of truth
    for trend math. Returns how many were recorded."""
    n = 0
    for key in _FACT_KEYS:
        if key not in (facts or {}):
            continue
        for item in (facts[key] if isinstance(facts[key], list) else [facts[key]]):
            unit = "kg" if "kg" in key else ("lb" if "lb" in key else None)
            parsed = None
            if isinstance(item, (int, float)):
                value = float(item)
                unit = unit or _infer_unit(value, (latest_weight(uid) or {}).get("weight_kg"))
                parsed = {"weight_kg": _to_kg(value, unit), "unit": unit}
            elif isinstance(item, str):
                p = parse_weight(item, (latest_weight(uid) or {}).get("weight_kg"))
                if not p and re.fullmatch(r"\s*\d{2,3}(\.\d+)?\s*", item):
                    value = float(item)
                    u = unit or _infer_unit(value, (latest_weight(uid) or {}).get("weight_kg"))
                    p = {"weight_kg": _to_kg(value, u), "unit": u}
                parsed = p
            elif isinstance(item, dict):
                for k in ("weight_lb", "weight_kg", "weight", "value"):
                    if isinstance(item.get(k), (int, float)):
                        value = float(item[k])
                        u = "kg" if "kg" in k else (unit or _infer_unit(
                            value, (latest_weight(uid) or {}).get("weight_kg")))
                        parsed = {"weight_kg": _to_kg(value, u), "unit": u,
                                  "measured_at": item.get("date") or item.get("measured_at")}
                        break
            if parsed and _MIN_KG <= parsed["weight_kg"] <= _MAX_KG:
                record_weight(uid, parsed["weight_kg"], parsed.get("unit") or "lb",
                              source="coach", measured_at=parsed.get("measured_at"))
                n += 1
    return n
