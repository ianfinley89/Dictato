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
import json

from app.database import get_conn
from app.services.ai import MODEL_HAIKU, _client
from app.services.ai_usage import record_tokens
from app.services.food_lookup import search_foods, get_food_by_id
from app.services.logging import log_entry_for_user, FoodNotFound
from app.services.voice_parse import parse_local

MAX_TURNS = 8
_MAX_NOTE = 200

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}

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
with servings. Otherwise use realistic typical portions in grams.
5. Only when no candidate fits: web_search for published nutrition (best for \
brand/restaurant items) and create_food with basis='web' plus the source_url. As a \
true last resort create_food with basis='estimate'.
6. NEVER invent nutrition numbers when a database candidate fits — the database is \
the source of truth.
7. Log every item with log_food BEFORE you write your final message. You CANNOT ask \
the user anything — your reply is shown only after logging finishes, and the user \
can undo or adjust afterwards. When a portion is unclear (e.g. a product photo), \
assume ONE typical serving per item and log it.
8. Your final message must be exactly ONE short, friendly sentence saying what you \
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
    nutrients = {
        "calories": _clamp(_num(inp.get("calories")) * factor, 0, 900),
        "protein_g": _clamp(_num(inp.get("protein_g")) * factor, 0, 100),
        "carbs_g": _clamp(_num(inp.get("carbs_g")) * factor, 0, 100),
        "fat_g": _clamp(_num(inp.get("fat_g")) * factor, 0, 100),
        "fiber_g": _clamp(_num(inp.get("fiber_g")) * factor, 0, 80),
    }
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


async def _execute_tool(name: str, inp: dict, user_id: int, method: str,
                        note: str | None, logged: list) -> dict:
    if name == "search_food_db":
        query = (inp.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        foods = await search_foods(query, user_id, limit=6, brand=(inp.get("brand") or None))
        return {"candidates": _fmt_candidates(foods)} if foods else {"candidates": [], "note": "no matches — try a simpler name, or web_search/create_food"}
    if name == "create_food":
        return _tool_create_food(user_id, inp)
    if name == "log_food":
        return _tool_log_food(user_id, inp, method, note, logged)
    return {"error": f"unknown tool {name}"}


async def run_agent(user_id: int, *, text: str | None = None,
                    image: bytes | None = None, image_media_type: str = "image/jpeg",
                    method: str = "voice") -> dict:
    """Run the logging loop. Returns {summary, entries, turns}."""
    import base64

    content: list[dict] = []
    if image:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": image_media_type,
            "data": base64.b64encode(image).decode("ascii")}})
    if text:
        content.append({"type": "text", "text": f"Transcript: {text}"})
    if image and not text:
        content.append({"type": "text", "text": "Log this meal photo."})

    note = (text or "photo")[:_MAX_NOTE]
    system = _SYSTEM + _user_context(user_id)
    tools = [_SEARCH_TOOL, _CREATE_TOOL, _LOG_TOOL, _WEB_SEARCH_TOOL]
    messages = [{"role": "user", "content": content}]
    client = _client()
    logged: list[dict] = []
    summary = ""

    error = False
    for turn in range(MAX_TURNS):
        try:
            resp = await client.messages.create(
                model=MODEL_HAIKU, max_tokens=1500, system=system,
                tools=tools, messages=messages,
            )
        except Exception:
            # Anything already logged this session stays logged — surface that
            # rather than losing it behind a 5xx.
            error = True
            break
        record_tokens(user_id, resp.usage.input_tokens, resp.usage.output_tokens)

        if resp.stop_reason == "pause_turn":   # server-side web_search continuing
            messages.append({"role": "assistant", "content": resp.content})
            continue

        text_parts = [b.text for b in resp.content if b.type == "text"]
        if resp.stop_reason != "tool_use":
            summary = " ".join(t.strip() for t in text_parts).strip()
            break

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = await _execute_tool(block.name, block.input, user_id, method, note, logged)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out)})
        if not results:
            break
        messages.append({"role": "user", "content": results})

    if not summary:
        if error:
            summary = (f"Hit a snag partway — {len(logged)} item{'s' if len(logged) != 1 else ''} did get logged."
                       if logged else "The logging agent hit an error. Please try again.")
        elif logged:
            summary = f"Logged {len(logged)} item{'s' if len(logged) != 1 else ''}."
        else:
            summary = "I couldn't identify anything to log — try rephrasing or log it manually."
    return {"summary": summary, "entries": logged, "turns": turn + 1, "error": error}


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
