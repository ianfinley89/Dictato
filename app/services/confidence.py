"""How much should we trust a capture — computed from FACTS, not self-report.

Asking the model for a confidence number does not work here. Measured on 123
Menu-Match items with dietitian ground truth, the model's own basis claims scored
AUROC 0.556 (noise): it asserted `label` for portions on voice-only orders and
`count` for counts it invented. A self-scored 0.0-1.0 would be the same unearned
claim in a new costume.

So every input below is something the server verified or observed:
  * did anything get logged at all
  * did the model bail and ask a question instead of logging
  * which ladder rung resolved each portion (verified in code, see portion.py)
  * did the guard have to clamp a wild guess
  * was the nutrition invented rather than looked up
  * did the food know its own serving size, so an estimate was at least bounded

Entries are weighted by their share of the capture's calories: a guessed 15 g
garnish barely matters, a guessed 800 kcal main is the whole log.

The score exists to decide ONE thing — whether to offer help ("say more" / type
it) on the result card. It must fire RARELY or it becomes wallpaper, which is why
low-stake captures are never flagged and a merely-bounded estimate is cheap.
"""
import re
from app.services import food_sources

# Nothing was logged, or the model asked a question instead of logging: not a
# judgement call, just a failed capture.
_FAILED = 0.0

_TRUSTED_BASES = {"stated", "label", "count"}       # evidence about THIS meal
_MEDIUM_BASES = {"household", "history"}            # grounded, but not stated

_RISK_UNANCHORED_ESTIMATE = 1.0   # a guess with nothing to bound it
_RISK_BOUNDED_ESTIMATE = 0.35     # a guess, but the food knows its serving size
_RISK_MEDIUM = 0.2
_RISK_SNAPPED = 0.3               # the guess had to be clamped, so it was wild
_RISK_INVENTED_NUTRITION = 0.5    # source='estimate': numbers, not a lookup

# Below this the capture is not worth interrupting anyone about.
MIN_STAKE_CALORIES = 250.0

_QUESTION_RE = re.compile(r"\?\s*$")


def _entry_risk(e: dict) -> float:
    basis = (e.get("portion_basis") or "estimate").lower()
    if basis in _TRUSTED_BASES:
        risk = 0.0
    elif basis in _MEDIUM_BASES:
        risk = _RISK_MEDIUM
    else:
        risk = (_RISK_BOUNDED_ESTIMATE if e.get("serving_g")
                else _RISK_UNANCHORED_ESTIMATE)
    if e.get("portion_snapped"):
        risk += _RISK_SNAPPED
    if (e.get("food_source_raw") or "") in food_sources.INVENTED:
        risk += _RISK_INVENTED_NUTRITION
    return min(1.0, risk)


def score_capture(entries: list[dict], summary: str = "") -> dict:
    """{score, reason, asked_question} — score 1.0 = fully grounded, 0.0 = failed."""
    if not entries:
        return {"score": _FAILED, "reason": "nothing-logged",
                "asked_question": bool(summary and _QUESTION_RE.search(summary))}
    if summary and _QUESTION_RE.search(summary):
        # The prompt forbids questions (the reply is only shown after logging),
        # but the model does it anyway and the user is left with a half-log.
        return {"score": _FAILED, "reason": "model-asked-a-question",
                "asked_question": True}

    cals = [max(0.0, float(e.get("calories") or 0)) for e in entries]
    total = sum(cals)
    weights = ([c / total for c in cals] if total > 0
               else [1.0 / len(entries)] * len(entries))
    risk = sum(_entry_risk(e) * w for e, w in zip(entries, weights))

    score = max(0.0, min(1.0, 1.0 - risk))
    reason = "guessed-portions" if risk > 0 else "grounded"
    # A trivial capture is never worth a prompt, however it was resolved.
    if total < MIN_STAKE_CALORIES:
        return {"score": max(score, 1.0), "reason": "low-stake",
                "asked_question": False}
    return {"score": round(score, 3), "reason": reason, "asked_question": False}


def needs_clarification(entries: list[dict], summary: str, threshold: float) -> dict:
    """Score plus the decision, so the caller doesn't re-implement the comparison."""
    s = score_capture(entries, summary)
    s["threshold"] = threshold
    s["clarify"] = s["score"] < threshold
    return s
