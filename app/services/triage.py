"""Auto-triage for issue reports: one tiny Haiku call sorts each report into
a pipeline the moment it arrives.

  infra   → a bug in the software itself (settings, UI, auth, notifications):
            the maintainer's fix-it queue.
  model   → the AI mis-handled food (wrong item, portion, brand, duplicate):
            these become training/eval dataset candidates — the report plus
            its linked capture (words, photo, audio, final labels) is exactly
            the example a better model learns from.
  capture → the mic/photo/transcription failed the user (heard "thank you"
            instead of speech, blurry photo): points at the capture pipeline
            or the device, and pairs with the saved audio for STT eval sets.
  other   → anything else.

Best-effort: a failed or nonsense model reply leaves the report uncategorized
(NULL) for the admin to label by hand in the dashboard. The call is traced
like every other model call (feature='issue_triage').
"""
import re

from app.services import llm

CATEGORIES = ("infra", "model", "capture", "other")

_SYSTEM = """You triage user bug reports for a food-logging app (users speak or \
photograph meals and an AI logs calories/macros). Reply with exactly one word:
- infra: a problem in the app software itself — settings, buttons, login, goals, \
notifications, pages not loading ("the goals setting won't save")
- model: the AI mis-logged food — wrong food, wrong portion/calories, wrong brand, \
missed or duplicated items ("I said 3 wings and it logged 85 grams")
- capture: the microphone, photo, or speech-to-text failed — it didn't hear them, \
transcribed the wrong words, or couldn't see the meal ("all it heard was 'thank you'")
- other: anything else (questions, feature requests, praise)"""


async def classify_issue(message: str, context: dict | None = None,
                         user_id: int | None = None) -> str | None:
    """Return one of CATEGORIES, or None when triage couldn't run."""
    ctx = context or {}
    parts = [f"Report: {message}"]
    if ctx.get("transcript"):
        parts.append(f'What the app heard: "{ctx["transcript"]}"')
    if ctx.get("summary"):
        parts.append(f"What the app logged: {ctx['summary']}")
    if ctx.get("from"):
        parts.append(f"Reported from: {ctx['from']}")
    try:
        resp = await llm.chat(feature="issue_triage", system=_SYSTEM,
                              messages=[{"role": "user", "text": "\n".join(parts)}],
                              max_tokens=16, user_id=user_id)
        for word in re.findall(r"[a-z]+", (resp.text or "").lower()):
            if word in CATEGORIES:
                return word
    except Exception:
        pass
    return None
