"""Agentic logging: one tool loop turns a transcript or meal photo into
grounded, saved log entries.

The model orchestrates but never supplies nutrition on its own authority:
it searches the food DB and picks the right candidate (replacing the old
top-hit heuristic), decomposes composite meals into ingredients, and may fall
back to web-published numbers (source='web') or a labeled last-resort estimate
(source='estimate'). Every logged entry's nutrition comes from a `foods` row.

A zero-cost fast path handles recurring items ("coffee", "2 rice cakes") the
user has logged before — no model call at all.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

from app.database import get_conn
from app.services import llm
from app.services.ai_usage import record_tokens
from app.services.food_lookup import search_foods, get_food_by_id
from app.services.logging import (log_entry_for_user, update_entry_quantity,
                                  remove_entry, FoodNotFound)
from app.services.nutrition_guard import sanitize_per_100g
from app.services.profile import apply_profile_update
from app.services.voice_parse import parse_local

MAX_TURNS = 8
_MAX_NOTE = 200

# Anthropic-only server-side search; the llm layer drops it on other providers.
_WEB_SEARCH_TOOL = {"server_tool": "web_search", "max_uses": 3}

_SEARCH_TOOL = {
    "name": "search_food_db",
    "description": (
        "Search the food database (local cache, USDA, Open Food Facts, FatSecret). "
        "Returns candidate foods with per-100g nutrition and serving size."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "plain food name, e.g. 'corn tortilla'"},
            "brand": {"type": "string", "description": "brand or restaurant, only if the user named one"},
        },
        "required": ["query"],
    },
}

_CREATE_TOOL = {
    "name": "create_food",
    "description": (
        "Create a food ONLY when no database candidate fits. Report the nutrition "
        "numbers exactly as published (or estimated) and set values_per to say "
        "whether they are per serving or per 100 g — the conversion happens "
        "server-side. basis='web': numbers you found with web_search (include "
        "source_url). basis='estimate': your own careful last-resort estimate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "brand": {"type": "string"},
            "serving_desc": {"type": "string", "description": "e.g. '1 burrito (300g)'"},
            "serving_g": {"type": "number", "description": "grams in one serving; required when values_per='serving'"},
            "values_per": {"type": "string", "enum": ["serving", "100g"],
                           "description": "what the calorie/macro numbers refer to"},
            "calories": {"type": "number"},
            "protein_g": {"type": "number"},
            "carbs_g": {"type": "number"},
            "fat_g": {"type": "number"},
            "fiber_g": {"type": "number"},
            "basis": {"type": "string", "enum": ["web", "estimate"]},
            "source_url": {"type": "string"},
        },
        "required": ["name", "values_per", "calories", "protein_g", "carbs_g", "fat_g", "basis"],
    },
}

_ANNOTATE_TOOL = {
    "name": "annotate_capture",
    "description": (
        "Call this exactly ONCE, after logging the items and before your final sentence. "
        "It labels this capture so the app and the coach can understand eating patterns, "
        "and it quietly remembers durable personal facts the user mentioned in passing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "meal": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "drink"],
                     "description": "based on the local time and what/how they described it"},
            "meal_label": {"type": "string",
                           "description": "2-4 word human name for the whole capture, e.g. 'cereal with berries'"},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "kebab-case, category-prefixed: activity:cycling, restaurant:taco-bell, "
                                    "drink:margarita, context:ate-out, context:post-workout; when a photo's "
                                    "portions were scaled off a known-size object, add scale:<object> "
                                    "(e.g. scale:fork, scale:soda-can)"},
            "specificity": {"type": "string", "enum": ["low", "medium", "high"],
                            "description": "how precise the user was about portions/brands — "
                                           "'a bite of taco' is low, 'two 110g Chipotle chicken tacos' is high"},
            "observed_facts": {"type": "object",
                               "description": "durable personal facts mentioned in passing (activities, life "
                                              "events, body context) e.g. {\"rides_bike\": true}. Omit if none."},
        },
        "required": ["meal", "meal_label", "specificity"],
    },
}

# Revision-mode tools: reconcile an existing log with new context.
_UPDATE_ENTRY_TOOL = {
    "name": "update_entry",
    "description": "Fix the quantity of an already-logged entry (grams).",
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "integer"},
            "quantity_g": {"type": "number"},
        },
        "required": ["entry_id", "quantity_g"],
    },
}

_REMOVE_ENTRY_TOOL = {
    "name": "remove_entry",
    "description": "Delete an already-logged entry that the new context shows was wrong.",
    "input_schema": {
        "type": "object",
        "properties": {"entry_id": {"type": "integer"}},
        "required": ["entry_id"],
    },
}

_REVISION_SYSTEM = """

REVISION MODE: the user already logged the meal below and is now ADDING context \
(another photo, or more words). This is additive — reconcile, never re-derive from \
scratch. Only change an entry when the new context gives CLEAR evidence it is wrong: \
a different food, more or fewer items, or a readable label that contradicts it. If \
the new context simply matches what's logged, change NOTHING and confirm it checks \
out. The user's original words stay authoritative for counts and portions unless the \
new context plainly contradicts them. Use update_entry to fix quantities, \
remove_entry to drop wrong items, and the normal search/log tools ONLY for items not \
logged yet — never re-log an existing item. If the label or tags improved, call \
annotate_capture again. Finish with one short sentence about what changed (or that \
it was confirmed) — no questions, no lists.

Already logged:
{entries}
They originally said: {transcript}"""

_LOG_TOOL = {
    "name": "log_food",
    "description": (
        "Log a food the user ate — saves immediately, call once per item. "
        "Give quantity_g (grams eaten), or servings when the food has a known serving_g."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "food_id": {"type": "integer"},
            "quantity_g": {"type": "number"},
            "servings": {"type": "number"},
        },
        "required": ["food_id"],
    },
}

_SYSTEM = """You are the food-logging agent inside Dictato, a calorie/macro tracker. \
The user just told you (voice transcript) or showed you (meal photo) what they ate. \
Identify each distinct food or drink, ground it in the food database, and LOG it.

Rules:
1. For every item call search_food_db first. Pass brand when the user names a brand or \
restaurant, and NEVER log a different brand than the one they named.
2. Pick the candidate that truly matches what was eaten. Prefer the user's own foods \
and recipes (listed below) when they fit.
3. Composite or homemade dishes with no good prepared-dish candidate: break the dish \
into its main ingredients, search each, and log each with realistic gram amounts for \
the stated portion.
4. Portions: when the user gives a count ("3 tacos") and the food has serving_g, log \
with servings. Otherwise use realistic typical portions in grams. Restaurant portions \
run LARGE and generic-database units run small: at a wing joint a "wing" is often a \
WHOLE wing (drum + flat, ~90-120g each, not a ~30g segment), a basket of fries is \
200-300g, a restaurant burrito 400g+. Size quantities for the restaurant's reality, \
never package/USDA defaults. In a PHOTO, if an object of known size is in frame \
(soda can, fork, credit card, standard ~27cm dinner plate), use it as a scale \
reference for portion sizes and NAME it in your final sentence ("judging by the \
fork, ..."); if none is present, estimate normally — never claim a reference you \
don't clearly see.
5. When a specific restaurant is named and there is no matching branded candidate, \
web_search that restaurant's item FIRST — portion style (whole vs segments, basket \
size, breaded or not) and published calorie estimates — then log the generic \
candidate with quantities matching what you learned, or create_food with basis='web'. \
Also use web_search when no candidate fits at all. As a true last resort create_food \
with basis='estimate'.
6. NEVER invent nutrition numbers when a database candidate fits — the database is \
the source of truth.
7. Log every item with log_food BEFORE you write your final message. You CANNOT ask \
the user anything — your reply is shown only after logging finishes, and the user \
can undo or adjust afterwards. When a portion is unclear (e.g. a product photo), \
assume ONE typical serving per item and log it.
8. After logging, call annotate_capture exactly once: the meal (use the local time \
and their words), a short human name for the whole capture, tags for anything \
notable (activities, restaurants, drinks, context), how SPECIFIC the user was, and \
any durable personal facts they mentioned in passing ("after I rode my bike to \
school" means they ride a bike — remember it silently, never comment on it).
9. Your final message must be exactly ONE short, friendly sentence saying what you \
logged (items and portions — no reasoning, no questions, no nutrition numbers, no \
markdown)."""


def _user_context(user_id: int) -> str:
    """Favorites + recently-logged foods, so recurring items resolve instantly."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT f.id, f.name, f.brand, f.serving_g FROM foods f
               WHERE f.id IN (
                 SELECT food_id FROM favorites WHERE user_id=:u
                 UNION
                 SELECT food_id FROM log_entries WHERE user_id=:u
                   AND eaten_at > datetime('now', '-14 days')
               )
               LIMIT 25""",
            {"u": user_id},
        ).fetchall()
    if not rows:
        return ""
    items = [
        {"food_id": r["id"], "name": r["name"],
         **({"brand": r["brand"]} if r["brand"] else {}),
         **({"serving_g": r["serving_g"]} if r["serving_g"] else {})}
        for r in rows
    ]
    return "\n\nThe user's favorite/recent foods (log these ids directly when they match):\n" + json.dumps(items)


def _fmt_candidates(foods: list[dict]) -> list[dict]:
    out = []
    for f in foods[:6]:
        n = f["nutrients_per_100g"]
        c = {
            "food_id": f["id"],
            "name": f["name"],
            "per_100g": {k: round(n.get(k) or 0, 1)
                         for k in ("calories", "protein_g", "carbs_g", "fat_g")},
            "source": f["source"],
        }
        if f.get("brand"):
            c["brand"] = f["brand"]
        if f.get("serving_g"):
            c["serving_g"] = f["serving_g"]
        if f.get("serving_desc"):
            c["serving_desc"] = f["serving_desc"]
        out.append(c)
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(v, lo, hi):
    return max(lo, min(hi, _num(v)))


def _tool_create_food(user_id: int, inp: dict) -> dict:
    name = (inp.get("name") or "").strip().lower()
    if not name:
        return {"error": "name is required"}
    basis = inp.get("basis")
    source = "web" if basis == "web" else "estimate"
    serving_g = inp.get("serving_g")
    serving_g = _clamp(serving_g, 1, 2000) if serving_g else None

    # Published numbers are usually per serving; the DB stores per 100 g.
    if inp.get("values_per") == "serving":
        if not serving_g:
            return {"error": "serving_g is required when values_per='serving'"}
        factor = 100.0 / serving_g
    else:
        factor = 1.0
    # Physical bounds on the macros, then the shared plausibility guard on energy
    # (kJ-as-kcal, impossible calories) — same treatment DB-cached foods get.
    nutrients, _ = sanitize_per_100g({
        "calories": _num(inp.get("calories")) * factor,
        "protein_g": _clamp(_num(inp.get("protein_g")) * factor, 0, 100),
        "carbs_g": _clamp(_num(inp.get("carbs_g")) * factor, 0, 100),
        "fat_g": _clamp(_num(inp.get("fat_g")) * factor, 0, 100),
        "fiber_g": _clamp(_num(inp.get("fiber_g")) * factor, 0, 80),
    })
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, source_id, name, brand, serving_desc, serving_g,
                                  nutrients_json, created_by_user_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (source, (inp.get("source_url") or "").strip() or None, name,
             (inp.get("brand") or "").strip().lower() or None,
             (inp.get("serving_desc") or "").strip() or None, serving_g,
             json.dumps(nutrients), user_id),
        )
        food_id = cur.lastrowid
    return {"food_id": food_id, "created": True, "source": source}


def _tool_log_food(user_id: int, inp: dict, method: str, note: str | None, logged: list) -> dict:
    food_id = inp.get("food_id")
    food = get_food_by_id(food_id) if isinstance(food_id, int) else None
    if not food:
        return {"error": f"food_id {food_id} not found — search or create it first"}
    servings = _num(inp.get("servings"))
    quantity_g = _num(inp.get("quantity_g"))
    if servings > 0 and food.get("serving_g"):
        quantity_g = servings * food["serving_g"]
    if quantity_g <= 0:
        return {"error": "provide quantity_g in grams (this food has no serving_g for servings)"}
    quantity_g = _clamp(quantity_g, 1, 5000)
    try:
        entry = log_entry_for_user(user_id, food_id, round(quantity_g, 1), method, notes=note)
    except FoodNotFound:
        return {"error": f"food_id {food_id} not found"}
    logged.append(entry)
    return {"logged": True, "entry_id": entry["id"], "name": entry["food_name"],
            "quantity_g": entry["quantity_g"], "calories": entry["calories"]}


_MEALS = {"breakfast", "lunch", "dinner", "snack", "drink"}
_SPECIFICITIES = {"low", "medium", "high"}


def _tool_annotate(user_id: int, inp: dict, annotation: dict) -> dict:
    meal = inp.get("meal")
    annotation["meal"] = meal if meal in _MEALS else None
    annotation["meal_label"] = (inp.get("meal_label") or "").strip().lower()[:60] or None
    tags = inp.get("tags") or []
    annotation["tags"] = [str(t).strip().lower()[:40] for t in tags if str(t).strip()][:10]
    spec = inp.get("specificity")
    annotation["specificity"] = spec if spec in _SPECIFICITIES else None
    facts = inp.get("observed_facts")
    if isinstance(facts, dict) and facts:
        apply_profile_update(user_id, facts)
    return {"annotated": True}


def _tool_update_entry(user_id: int, inp: dict) -> dict:
    entry_id = inp.get("entry_id")
    quantity_g = _clamp(inp.get("quantity_g"), 1, 5000)
    if not isinstance(entry_id, int) or quantity_g <= 0:
        return {"error": "entry_id and a positive quantity_g are required"}
    try:
        e = update_entry_quantity(user_id, entry_id, round(quantity_g, 1))
    except FoodNotFound:
        return {"error": f"entry {entry_id} not found"}
    return {"updated": True, "entry_id": entry_id,
            "quantity_g": e["quantity_g"], "calories": e["calories"]}


def _tool_remove_entry(user_id: int, inp: dict) -> dict:
    entry_id = inp.get("entry_id")
    try:
        remove_entry(user_id, entry_id)
    except FoodNotFound:
        return {"error": f"entry {entry_id} not found"}
    return {"removed": True, "entry_id": entry_id}


async def _execute_tool(name: str, inp: dict, user_id: int, method: str,
                        note: str | None, logged: list, annotation: dict,
                        existing_food_ids: dict | None = None) -> dict:
    if name == "search_food_db":
        query = (inp.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        foods = await search_foods(query, user_id, limit=6, brand=(inp.get("brand") or None))
        return {"candidates": _fmt_candidates(foods)} if foods else {"candidates": [], "note": "no matches — try a simpler name, or web_search/create_food"}
    if name == "create_food":
        return _tool_create_food(user_id, inp)
    if name == "log_food":
        # Revision guard: the same food again means adjust, not duplicate.
        fid = inp.get("food_id")
        if existing_food_ids and fid in existing_food_ids:
            return {"error": f"food_id {fid} is already logged as entry "
                             f"{existing_food_ids[fid]} — use update_entry with the "
                             f"new TOTAL grams instead of logging it again"}
        return _tool_log_food(user_id, inp, method, note, logged)
    if name == "annotate_capture":
        return _tool_annotate(user_id, inp, annotation)
    if name == "update_entry":
        return _tool_update_entry(user_id, inp)
    if name == "remove_entry":
        return _tool_remove_entry(user_id, inp)
    return {"error": f"unknown tool {name}"}


def local_hour_meal(tz_offset: int) -> str:
    """Fallback meal bucket by the user's local hour (same rules as the UI)."""
    h = (datetime.now(timezone.utc) - timedelta(minutes=max(-840, min(840, tz_offset)))).hour
    if 5 <= h < 11:
        return "breakfast"
    if 11 <= h < 16:
        return "lunch"
    if 16 <= h < 22:
        return "dinner"
    return "snack"


async def run_agent(user_id: int, *, text: str | None = None,
                    image: bytes | None = None, image_media_type: str = "image/jpeg",
                    method: str = "voice", tz_offset: int = 0,
                    revision: dict | None = None) -> dict:
    """Run the logging loop. `revision` = {entries, transcript} switches to
    reconcile-an-existing-log mode. Returns {summary, entries, annotation, turns}."""
    user_text = None
    if text:
        user_text = f"Transcript: {text}"
    elif image:
        user_text = "Log this meal photo." if not revision else "Here's a photo of the meal I just logged."
    user_msg = {"role": "user", "text": user_text}
    if image:
        user_msg["images"] = [{"media_type": image_media_type,
                               "data_b64": base64.b64encode(image).decode("ascii")}]

    local_now = datetime.now(timezone.utc) - timedelta(minutes=max(-840, min(840, tz_offset)))
    note = (text or "photo")[:_MAX_NOTE]
    system = (_SYSTEM + f"\n\nUser's local time: {local_now.strftime('%A %H:%M')}."
              + _user_context(user_id))
    tools = [_SEARCH_TOOL, _CREATE_TOOL, _LOG_TOOL, _ANNOTATE_TOOL, _WEB_SEARCH_TOOL]
    existing_food_ids = None
    if revision:
        prior = [{"entry_id": e["id"], "name": e["food_name"],
                  "quantity_g": e["quantity_g"], "calories": e["calories"]}
                 for e in revision.get("entries", [])]
        system += _REVISION_SYSTEM.format(
            entries=json.dumps(prior),
            transcript=json.dumps(revision.get("transcript") or "(photo, no words)"))
        tools = tools + [_UPDATE_ENTRY_TOOL, _REMOVE_ENTRY_TOOL]
        existing_food_ids = {e["food_id"]: e["id"] for e in revision.get("entries", [])}
    messages = [user_msg]
    logged: list[dict] = []
    annotation: dict = {}
    summary = ""

    # Route photo captures to the vision model, voice/text to the text model.
    feature = "photo" if image else "voice"
    error = False
    for turn in range(MAX_TURNS):
        try:
            resp = await llm.chat(feature=feature, system=system, messages=messages,
                                  tools=tools, max_tokens=1500, user_id=user_id)
        except Exception:
            # Anything already logged this session stays logged — surface that
            # rather than losing it behind a 5xx.
            error = True
            break
        record_tokens(user_id, resp.input_tokens, resp.output_tokens)

        if resp.stop_reason == "pause":   # server-side web_search continuing
            messages.append(resp.assistant_message())
            continue

        if resp.stop_reason != "tool_use":
            summary = resp.text
            break

        messages.append(resp.assistant_message())
        results = []
        for tc in resp.tool_calls:
            out = await _execute_tool(tc.name, tc.input, user_id, method, note,
                                      logged, annotation, existing_food_ids)
            results.append({"id": tc.id, "name": tc.name, "content": json.dumps(out)})
        if not results:
            break
        messages.append({"role": "tool", "results": results})

    if not summary:
        if error:
            summary = (f"Hit a snag partway — {len(logged)} item{'s' if len(logged) != 1 else ''} did get logged."
                       if logged else "The logging agent hit an error. Please try again.")
        elif logged:
            summary = f"Logged {len(logged)} item{'s' if len(logged) != 1 else ''}."
        else:
            summary = "I couldn't identify anything to log — try rephrasing or log it manually."
    if logged and not annotation.get("meal"):
        annotation.setdefault("meal", local_hour_meal(tz_offset))
    return {"summary": summary, "entries": logged, "annotation": annotation,
            "turns": turn + 1, "error": error}


def fast_path_annotation(transcript: str, entries: list[dict], tz_offset: int = 0) -> dict:
    """No-model annotation for fast-path captures: meal from the local hour,
    label from the food names, specificity from how the phrase parsed."""
    items = parse_local(transcript) or []
    conf = min((i["confidence"] for i in items), default=0.5)
    names = [e["food_name"] for e in entries][:2]
    return {
        "meal": local_hour_meal(tz_offset),
        "meal_label": " + ".join(n.lower() for n in names)[:60] or None,
        "tags": [],
        "specificity": "high" if conf >= 0.85 else ("medium" if conf >= 0.5 else "low"),
    }


def fast_path_log(user_id: int, transcript: str, method: str = "voice") -> list[dict] | None:
    """Zero-cost path for recurring items: every parsed item must exactly match a
    food this user has logged before, favorited, or created. Returns the logged
    entries, or None to fall through to the agent."""
    items = parse_local(transcript)
    if not items:
        return None

    resolved = []
    with get_conn() as conn:
        for item in items:
            if item.get("brand") or not item["name"]:
                return None
            # naive singular/plural so "two rice cakes" matches "rice cake"
            name = item["name"]
            variants = {name, name + "s", name[:-1] if name.endswith("s") else name}
            row = conn.execute(
                f"""SELECT f.id, f.serving_g FROM foods f
                   WHERE lower(f.name) IN ({",".join("?" * len(variants))}) AND f.id IN (
                     SELECT food_id FROM favorites WHERE user_id=?
                     UNION SELECT food_id FROM log_entries WHERE user_id=?
                     UNION SELECT id FROM foods
                       WHERE created_by_user_id=? AND source IN ('user','recipe')
                   )
                   ORDER BY f.serving_g IS NULL LIMIT 1""",
                (*variants, user_id, user_id, user_id),
            ).fetchone()
            if not row:
                return None
            # Only log quantities we can trust: a count needs the food's real
            # serving size; otherwise explicit grams/volume. Anything vaguer
            # ("a rice cake" with no serving_g would default to 100g) goes to
            # the agent, which reasons out a realistic portion instead.
            servings = item.get("est_servings")
            if servings is not None:
                if not row["serving_g"]:
                    return None
                quantity_g = servings * row["serving_g"]
            elif item["confidence"] >= 0.75:   # explicit measure like "100g oats"
                quantity_g = item["est_quantity_g"]
            else:
                return None
            resolved.append((row["id"], quantity_g))

    note = transcript[:_MAX_NOTE]
    return [log_entry_for_user(user_id, fid, round(q, 1), method, notes=note)
            for fid, q in resolved]
