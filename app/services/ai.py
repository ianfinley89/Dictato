"""Shared Anthropic helpers for voice text-parsing and photo vision.

Both paths return the same structured item shape so the frontend can reuse one
resolve→confirm→log pipeline. Per hard rule #1, the model only identifies and
estimates; nutrition always comes from the food DB afterwards.
"""
import base64
from app.config import ANTHROPIC_API_KEY
from app.services.ai_usage import record_tokens
from app.services.voice_parse import _UNIT_TO_G

MODEL_HAIKU = "claude-haiku-4-5-20251001"

PARSE_TOOL = {
    "name": "parse_food_items",
    "description": "Extract the distinct food/drink items and quantity estimates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One short, friendly sentence describing what you identified and the "
                    "portions, e.g. 'I see a California roll, about 10 pieces.' "
                    "Do NOT mention calories or nutrition numbers."
                ),
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":         {"type": "string"},
                        "est_quantity": {"type": "number"},
                        "unit":         {"type": "string", "description": "g, oz, cup, slice, piece, tbsp, tsp, scoop, serving"},
                        "confidence":   {"type": "number", "description": "0.0-1.0"},
                    },
                    "required": ["name", "est_quantity", "unit", "confidence"],
                },
            },
        },
        "required": ["summary", "items"],
    },
}


def _client():
    import anthropic
    return anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def parse_text(transcript: str, user_id: int) -> dict:
    content = (
        "Extract food items from this voice transcript. Give realistic quantity "
        f"estimates.\n\nTranscript: {transcript}"
    )
    return await _run(content, user_id)


async def parse_image(image_bytes: bytes, media_type: str, user_id: int) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": (
            "Identify each distinct food or drink in this meal photo and estimate "
            "the portion of each. Be specific (e.g. 'grilled chicken breast', not 'meat')."
        )},
    ]
    return await _run(content, user_id)


_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
_REPORT_TOOL = {
    "name": "report_nutrition",
    "description": "Report the published nutrition facts found in the search results.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found":     {"type": "boolean", "description": "true only if reliable published values were found"},
            "name":      {"type": "string"},
            "serving":   {"type": "string", "description": "e.g. '1 burrito', '1 can'"},
            "calories":  {"type": "number"},
            "protein_g": {"type": "number"},
            "carbs_g":   {"type": "number"},
            "fat_g":     {"type": "number"},
            "source_url": {"type": "string"},
        },
        "required": ["found"],
    },
}


async def lookup_nutrition_web(name: str, brand: str | None, user_id: int) -> dict:
    """Two passes: (1) web-search the published nutrition, (2) extract it as
    structured JSON. A verifiable DRAFT for the user — never auto-logged (rules #1/#5)."""
    label = f"{name} from {brand}" if brand else name
    client = _client()

    # Pass 1 — search the web (returns prose + citations).
    messages = [{"role": "user", "content": (
        f"Search the web for the official published nutrition facts of: {label}. "
        "Prefer the brand/restaurant's own site or a reputable nutrition database. "
        "Find calories, protein, carbs, and fat for one standard serving, and the source URL."
    )}]
    for _ in range(4):   # follow pause_turn continuations
        resp = await client.messages.create(
            model=MODEL_HAIKU, max_tokens=1024, tools=[_WEB_SEARCH_TOOL], messages=messages,
        )
        record_tokens(user_id, resp.usage.input_tokens, resp.usage.output_tokens)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})

    # Pass 2 — force structured extraction from those results.
    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": (
        "Using the search results above, call report_nutrition for ONE standard serving. "
        "If sources give a range or values vary by customization, use the most representative "
        "standard value rather than giving up. Set found=false ONLY if the results contain no "
        "nutrition numbers at all."
    )})
    resp2 = await client.messages.create(
        model=MODEL_HAIKU, max_tokens=400,
        tools=[_WEB_SEARCH_TOOL, _REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report_nutrition"},
        messages=messages,
    )
    record_tokens(user_id, resp2.usage.input_tokens, resp2.usage.output_tokens)

    data = next((b.input for b in resp2.content if b.type == "tool_use" and b.name == "report_nutrition"), None)
    if not data or not data.get("found"):
        return {"found": False}
    return {
        "found": True,
        "name": (data.get("name") or name).strip(),
        "serving": (data.get("serving") or "1 serving").strip(),
        "calories": _num(data.get("calories")),
        "protein_g": _num(data.get("protein_g")),
        "carbs_g": _num(data.get("carbs_g")),
        "fat_g": _num(data.get("fat_g")),
        "source_url": (data.get("source_url") or "").strip(),
    }


def _num(v) -> float:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return 0.0


async def _run(content, user_id: int) -> dict:
    """Returns {"items": [...], "summary": str}."""
    resp = await _client().messages.create(
        model=MODEL_HAIKU,
        max_tokens=512,
        tools=[PARSE_TOOL],
        tool_choice={"type": "tool", "name": PARSE_TOOL["name"]},
        messages=[{"role": "user", "content": content}],
    )
    record_tokens(user_id, resp.usage.input_tokens, resp.usage.output_tokens)
    for block in resp.content:
        if block.type == "tool_use" and block.name == PARSE_TOOL["name"]:
            data = block.input
            return {
                "items": [normalise(i) for i in data.get("items", [])],
                "summary": (data.get("summary") or "").strip(),
            }
    return {"items": [], "summary": ""}


_COUNT_UNITS = {"serving", "piece", "can", "count", "item", "unit"}


def normalise(item: dict) -> dict:
    """Convert a model item to {name, est_quantity_g, est_servings, unit, confidence}."""
    unit = (item.get("unit") or "g").lower().rstrip("s")
    qty = float(item.get("est_quantity", 100))
    qty_g = qty * _UNIT_TO_G.get(unit, 100)
    # When the model counts whole servings/items, keep the count so the food's
    # known serving size can be applied at confirm time.
    servings = qty if unit in _COUNT_UNITS else None
    return {
        "name": (item.get("name") or "").strip().lower(),
        "brand": (item.get("brand") or "").strip().lower() or None,
        "est_quantity_g": round(qty_g, 1),
        "est_servings": servings,
        "unit": unit,
        "confidence": float(item.get("confidence", 0.7)),
    }
