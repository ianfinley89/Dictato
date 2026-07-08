"""Shared Anthropic helpers.

The agentic logging loop lives in app/services/agent.py; this module keeps the
client factory and the two-pass web nutrition lookup used by the manual
"look it up on the web" flow (/api/foods/weblookup).
"""
from app.config import ANTHROPIC_API_KEY
from app.services.ai_usage import record_tokens

MODEL_HAIKU = "claude-haiku-4-5-20251001"


def _client():
    import anthropic
    return anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


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


