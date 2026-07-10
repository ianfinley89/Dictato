"""The coach: an on-demand chat that reads the user's own data — food logs,
voice/photo capture transcripts, goals, and an accumulated profile — and offers
grounded, gentle suggestions.

It is a *good listener*: whenever the user reveals a durable fact in passing
(a weigh-in, their sex/age, a training style, a goal, a food they dislike), the
model calls `update_profile` to remember it, so the picture of the person fills
in over time with zero extra effort from them.

Nothing here invents food nutrition — that stays the logger's job. The coach
only reasons over what was already logged and said.
"""
import json

from app.database import get_conn
from app.services import llm
from app.services.ai_usage import record_tokens
from app.services.profile import get_profile, merge_facts, apply_profile_update

# Back-compat aliases (tests and older callers use the underscored names).
_merge_facts = merge_facts
_apply_profile_update = apply_profile_update

_MAX_TURNS = 3
_HISTORY_LIMIT = 10
_NOTES_LIMIT = 20

_UPDATE_PROFILE_TOOL = {
    "name": "update_profile",
    "description": (
        "Remember durable facts about the user that you learned from what they said — "
        "e.g. sex, age, height, a weigh-in, training style, their goal (cut/bulk/maintain), "
        "dietary preferences, restrictions, or injuries. Only include keys you are adding or "
        "changing; existing facts are kept. For recurring measurements like weigh-ins, add to a "
        "list, e.g. {\"weigh_ins\": [{\"date\": \"2026-07-08\", \"lbs\": 182}]}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {"type": "object", "description": "flat JSON of facts to merge into the profile"},
        },
        "required": ["facts"],
    },
}

_SYSTEM = """You are the Dictato coach — a warm, encouraging, evidence-based nutrition and \
fitness companion. You are talking with the user about their eating and progress.

Ground everything in the DATA below (their goals, recent daily totals, recent notes with meal \
tags, and profile). When the data is thin or missing, say so plainly and ask ONE friendly \
question rather than guessing. Never invent specific calorie/macro numbers for foods — those \
come from their log. Notes marked low specificity are rough estimates; weight them accordingly.

Be a good listener: when the user mentions a durable fact about themselves (a weigh-in, their \
sex/age/height, training style, their goal, foods they like or avoid, injuries), call \
update_profile to remember it. Don't announce that you're saving it — just weave it in naturally.

NEVER assume a typical body or life. Check the profile first: pregnancy or postpartum, \
disability, illness, unusual height or weight, age — any of these change what good advice is. \
If a recommendation depends on a fact you don't have, ask instead of assuming. When calorie or \
macro targets come up, use calc_targets to compute them from their actual stats — never estimate \
those numbers yourself, and if stats are missing, ask for them. You cannot change their goals; \
when you recommend new targets, show the numbers and point them to Settings → Set goals.

Give specific, kind, actionable suggestions, and connect them to what you see (e.g. "your protein \
has averaged 90g against your 150g goal — adding a scoop of whey would close most of that gap"). \
Keep replies short and conversational (2-4 sentences unless they ask for detail). You are not a \
doctor; for medical concerns, gently suggest a professional. No markdown headings."""


_CALC_TARGETS_TOOL = {
    "name": "calc_targets",
    "description": (
        "Compute daily calorie/protein targets from the user's actual stats "
        "(Mifflin-St Jeor BMR × activity, adjusted for their goal). Use this whenever "
        "targets come up — never estimate these numbers yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sex": {"type": "string", "enum": ["male", "female"]},
            "age": {"type": "number"},
            "height_cm": {"type": "number"},
            "height_in": {"type": "number", "description": "use if height is known in inches"},
            "weight_kg": {"type": "number"},
            "weight_lbs": {"type": "number", "description": "use if weight is known in pounds"},
            "activity_level": {"type": "string",
                               "enum": ["sedentary", "light", "moderate", "very_active"]},
            "goal": {"type": "string", "enum": ["cut", "maintain", "bulk"]},
        },
        "required": ["sex", "age", "activity_level", "goal"],
    },
}

_ACTIVITY_FACTOR = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "very_active": 1.725}
_GOAL_DELTA = {"cut": -400, "maintain": 0, "bulk": 300}


def calc_targets(inp: dict) -> dict:
    """Deterministic target math — the formula supplies numbers, the model supplies judgment."""
    height_cm = inp.get("height_cm") or (inp.get("height_in") or 0) * 2.54
    weight_kg = inp.get("weight_kg") or (inp.get("weight_lbs") or 0) * 0.4536
    if not height_cm or not weight_kg:
        return {"error": "need height (cm or in) and weight (kg or lbs)"}
    age = float(inp.get("age") or 0)
    if not (10 <= age <= 110):
        return {"error": "need a plausible age"}
    sex_term = 5 if inp.get("sex") == "male" else -161
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + sex_term
    tdee = bmr * _ACTIVITY_FACTOR.get(inp.get("activity_level"), 1.375)
    goal = inp.get("goal", "maintain")
    return {
        "bmr": round(bmr),
        "tdee": round(tdee),
        "suggested_calories": round(tdee + _GOAL_DELTA.get(goal, 0)),
        "suggested_protein_g": round(1.8 * weight_kg),   # 1.8 g/kg — solid general target
        "basis": f"Mifflin-St Jeor, {inp.get('activity_level')} activity, {goal}",
    }


def get_history(uid: int, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM coach_messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _save_message(uid: int, role: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO coach_messages (user_id, role, content) VALUES (?,?,?)",
            (uid, role, content),
        )


def _build_context(uid: int, profile: dict) -> str:
    """A compact, model-readable snapshot of everything the coach may reason over."""
    with get_conn() as conn:
        u = conn.execute(
            "SELECT display_name, calorie_goal, protein_g, carbs_g, fat_g FROM users WHERE id=?",
            (uid,),
        ).fetchone()
        days = conn.execute(
            """SELECT DATE(eaten_at) AS d,
                      ROUND(SUM(json_extract(nutrients_snapshot_json,'$.calories')),0)  AS cal,
                      ROUND(SUM(json_extract(nutrients_snapshot_json,'$.protein_g')),0) AS p,
                      ROUND(SUM(json_extract(nutrients_snapshot_json,'$.carbs_g')),0)   AS c,
                      ROUND(SUM(json_extract(nutrients_snapshot_json,'$.fat_g')),0)     AS f
               FROM log_entries
               WHERE user_id=? AND DATE(eaten_at) >= DATE('now','-14 days')
               GROUP BY d ORDER BY d""",
            (uid,),
        ).fetchall()
        notes = conn.execute(
            """SELECT DATE(created_at) AS d, transcript, meal, meal_label, tags_json, specificity
               FROM capture_log
               WHERE user_id=? AND transcript IS NOT NULL AND transcript != ''
               ORDER BY id DESC LIMIT ?""",
            (uid, _NOTES_LIMIT),
        ).fetchall()

    goals = {k: u[k] for k in ("calorie_goal", "protein_g", "carbs_g", "fat_g")} if u else {}
    daily = [
        {"date": r["d"], "calories": r["cal"], "protein_g": r["p"], "carbs_g": r["c"], "fat_g": r["f"]}
        for r in days
    ]
    recent_notes = []
    for r in reversed(notes):
        n = {"date": r["d"], "said": r["transcript"]}
        if r["meal"]:
            n["meal"] = r["meal"]
        if r["meal_label"]:
            n["label"] = r["meal_label"]
        try:
            tags = json.loads(r["tags_json"] or "[]")
            if tags:
                n["tags"] = tags
        except json.JSONDecodeError:
            pass
        if r["specificity"]:
            n["specificity"] = r["specificity"]
        recent_notes.append(n)

    return (
        f"USER: {u['display_name'] if u else 'there'}\n"
        f"GOALS (per day): {json.dumps(goals)}\n"
        f"PROFILE (facts remembered so far): {json.dumps(profile) if profile else '(nothing yet)'}\n"
        f"DAILY TOTALS (last 14 days): {json.dumps(daily) if daily else '(no food logged yet)'}\n"
        f"RECENT NOTES (what they said when logging): {json.dumps(recent_notes) if recent_notes else '(none)'}"
    )


async def chat(uid: int, message: str) -> dict:
    """Run one coach turn. Persists both sides of the exchange and any profile
    facts the model chose to remember. Returns {reply, profile}."""
    profile = get_profile(uid)
    system = _SYSTEM + "\n\n=== DATA ===\n" + _build_context(uid, profile)

    messages = [{"role": m["role"], "text": m["content"]} for m in get_history(uid, _HISTORY_LIMIT)]
    messages.append({"role": "user", "text": message})

    reply = ""
    for _ in range(_MAX_TURNS):
        resp = await llm.chat(feature="coach", system=system, messages=messages,
                              tools=[_UPDATE_PROFILE_TOOL, _CALC_TARGETS_TOOL],
                              max_tokens=800, user_id=uid)
        record_tokens(uid, resp.input_tokens, resp.output_tokens)

        if resp.tool_calls:
            messages.append(resp.assistant_message())
            results = []
            for tc in resp.tool_calls:
                if tc.name == "update_profile":
                    apply_profile_update(uid, tc.input.get("facts") or {})
                    out = "saved"
                elif tc.name == "calc_targets":
                    out = json.dumps(calc_targets(tc.input))
                else:
                    out = "unknown tool"
                results.append({"id": tc.id, "name": tc.name, "content": out})
            messages.append({"role": "tool", "results": results})
            continue   # let the model finish with a spoken reply
        reply = resp.text
        break

    if not reply:
        reply = "Got it — noted. Tell me more, or ask me anything about how you're tracking."

    _save_message(uid, "user", message)
    _save_message(uid, "assistant", reply)
    return {"reply": reply, "profile": get_profile(uid)}
