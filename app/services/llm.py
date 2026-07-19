"""One swappable chat interface for the agent and coach.

Callers work in a small *neutral* format and never touch a provider SDK:

  messages = [
    {"role": "user", "text": "...", "images": [{"media_type", "data_b64"}]},
    resp.assistant_message(),                       # opaque passthrough of a prior turn
    {"role": "tool", "results": [{"id", "name", "content"}]},
  ]
  tools = [ {name, description, input_schema}, {"server_tool": "web_search"} ]
  resp = await chat(feature="agent", system=..., messages=messages, tools=tools)
  resp.text, resp.tool_calls[i].(id|name|input), resp.stop_reason  # 'tool_use'|'end'|'pause'

`LLM_PROVIDER=anthropic` (default) uses the Anthropic SDK — identical behavior to
before, including the server-side web_search tool. `LLM_PROVIDER=openai` routes
through any OpenAI-compatible endpoint (OpenRouter for DeepSeek/Qwen/Gemini, or a
local Ollama/vLLM server for Gemma); the web_search server tool is dropped there,
since it has no OpenAI-format equivalent.
"""
import json
import time
from dataclasses import dataclass, field

from app import config
from app.database import get_conn

# Cheapest Anthropic vision model; the default when provider=anthropic.
MODEL_HAIKU = "claude-haiku-4-5-20251001"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list          # list[ToolCall]
    stop_reason: str          # 'tool_use' | 'end' | 'pause'
    input_tokens: int
    output_tokens: int
    raw: object = field(default=None, repr=False)   # provider-native assistant turn, for faithful continuation

    def assistant_message(self) -> dict:
        """The assistant turn to append back before the next chat() call."""
        return {"role": "assistant", "raw": self.raw}


def _model_for(feature: str) -> str:
    override = {"agent": config.AGENT_MODEL, "coach": config.COACH_MODEL}.get(feature, "")
    if override:
        return override
    if config.LLM_MODEL:
        return config.LLM_MODEL
    return MODEL_HAIKU   # anthropic default


_KNOWN_PROVIDERS = {"anthropic", "openrouter", "local"}


def _endpoint(provider: str):
    """(kind, base_url, api_key) for a named provider. kind ∈ 'anthropic'|'openai'."""
    if provider == "openrouter":
        return "openai", config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY
    if provider == "local":
        return "openai", config.LOCAL_BASE_URL, config.LOCAL_API_KEY
    if provider == "openai":                       # legacy global endpoint
        return "openai", config.LLM_BASE_URL, config.LLM_API_KEY
    return "anthropic", "", config.ANTHROPIC_API_KEY


def _feature_spec(feature: str) -> str:
    return {
        "voice": config.VOICE_MODEL or config.AGENT_MODEL,
        "agent": config.AGENT_MODEL or config.VOICE_MODEL,
        "photo": config.PHOTO_MODEL,
        "coach": config.COACH_MODEL,
        "issue_triage": config.TRIAGE_MODEL,
    }.get(feature, "")


def _resolve_feature(feature: str):
    """Route a feature → (kind, model, base_url, api_key). A per-feature value may
    be 'provider:model' (provider ∈ anthropic/openrouter/local — a bare colon in
    the model id like ':free' is preserved) or a bare model id (global provider).
    Unset → the global default, EXCEPT 'photo', which defaults to a vision-capable
    Anthropic model so photos never break when text features are pointed at a
    non-vision model like DeepSeek."""
    spec = (_feature_spec(feature) or "").strip()
    if spec:
        head, sep, rest = spec.partition(":")
        if sep and rest and head in _KNOWN_PROVIDERS:
            kind, base, key = _endpoint(head)
            return kind, rest, base, key
        if config.LLM_PROVIDER == "openai":        # bare model on the global endpoint
            kind, base, key = _endpoint("openai")
            return kind, spec, base, key
        return "anthropic", spec, "", config.ANTHROPIC_API_KEY
    if feature == "photo":                          # vision safety net
        return "anthropic", MODEL_HAIKU, "", config.ANTHROPIC_API_KEY
    if config.LLM_PROVIDER == "openai":
        kind, base, key = _endpoint("openai")
        return kind, (config.LLM_MODEL or MODEL_HAIKU), base, key
    return "anthropic", MODEL_HAIKU, "", config.ANTHROPIC_API_KEY


def _sanitize_messages(messages: list) -> list:
    """A JSON-safe snapshot of the request for tracing: image payloads redacted,
    provider-native blocks stringified. Never raises."""
    out = []
    for m in messages:
        try:
            if "raw" in m:
                out.append({"role": "assistant", "raw": "[assistant turn]"})
            elif m.get("role") == "tool":
                out.append({"role": "tool",
                            "results": [{"name": r.get("name"), "content": str(r.get("content"))[:2000]}
                                        for r in m.get("results", [])]})
            else:
                entry = {"role": m.get("role"), "text": (m.get("text") or "")[:4000]}
                if m.get("images"):
                    entry["images"] = [f"[{img.get('media_type')}, {len(img.get('data_b64') or '')} b64 chars]"
                                       for img in m["images"]]
                out.append(entry)
        except Exception:
            out.append({"role": "?", "text": "[unserializable]"})
    return out


def _record_trace(*, user_id, feature, provider, model, latency_ms, system, messages,
                  resp: "LLMResponse | None" = None, error: str | None = None) -> None:
    """Best-effort tracing — a telemetry failure must never break the model call."""
    try:
        request_json = json.dumps({"system": system[:4000], "messages": _sanitize_messages(messages)},
                                  default=str)
        response_json = None
        if resp is not None:
            response_json = json.dumps({
                "text": resp.text[:6000],
                "tool_calls": [{"name": tc.name, "input": tc.input} for tc in resp.tool_calls],
            }, default=str)
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO model_traces
                   (user_id, feature, provider, model, latency_ms, input_tokens, output_tokens,
                    stop_reason, error, request_json, response_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, feature, provider, model, latency_ms,
                 resp.input_tokens if resp else None, resp.output_tokens if resp else None,
                 resp.stop_reason if resp else None, error, request_json, response_json),
            )
    except Exception:
        pass


async def chat(*, feature: str, system: str, messages: list,
               tools: list | None = None, max_tokens: int = 1024,
               user_id: int | None = None) -> LLMResponse:
    kind, model, _base, _key = _resolve_feature(feature)
    provider = "anthropic" if kind == "anthropic" else "openai"
    t0 = time.perf_counter()
    try:
        if kind == "anthropic":
            resp = await _chat_anthropic(feature, system, messages, tools, max_tokens)
        else:
            resp = await _chat_openai(feature, system, messages, tools, max_tokens)
    except Exception as e:
        _record_trace(user_id=user_id, feature=feature, provider=provider, model=model,
                      latency_ms=int((time.perf_counter() - t0) * 1000),
                      system=system, messages=messages, error=repr(e)[:800])
        raise
    _record_trace(user_id=user_id, feature=feature, provider=provider, model=model,
                  latency_ms=int((time.perf_counter() - t0) * 1000),
                  system=system, messages=messages, resp=resp)
    return resp


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _anthropic_tools(tools):
    out = []
    for t in tools or []:
        if t.get("server_tool") == "web_search":
            out.append({"type": "web_search_20250305", "name": "web_search",
                        "max_uses": t.get("max_uses", 3)})
        else:
            out.append({"name": t["name"], "description": t["description"],
                        "input_schema": t["input_schema"]})
    return out


def _anthropic_messages(messages):
    out = []
    for m in messages:
        if "raw" in m:                       # assistant passthrough
            out.append({"role": "assistant", "content": m["raw"]})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                for r in m["results"]]})
        elif m["role"] == "user":
            content = [{"type": "image", "source": {"type": "base64",
                        "media_type": img["media_type"], "data": img["data_b64"]}}
                       for img in m.get("images", [])]
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            out.append({"role": "user", "content": content})
        else:                                # neutral assistant (e.g. seeded history)
            content = []
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            for tc in m.get("tool_calls", []):
                content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            out.append({"role": "assistant", "content": content})
    return out


async def _chat_anthropic(feature, system, messages, tools, max_tokens):
    import anthropic
    _kind, model, _base, api_key = _resolve_feature(feature)
    client = anthropic.AsyncAnthropic(api_key=api_key or config.ANTHROPIC_API_KEY)
    kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=_anthropic_messages(messages))
    atools = _anthropic_tools(tools)
    if atools:
        kwargs["tools"] = atools
    resp = await client.messages.create(**kwargs)

    text = " ".join(b.text.strip() for b in resp.content if b.type == "text").strip()
    tool_calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
    if resp.stop_reason == "pause_turn":
        stop = "pause"
    elif tool_calls or resp.stop_reason == "tool_use":
        stop = "tool_use"
    else:
        stop = "end"
    return LLMResponse(text, tool_calls, stop,
                       resp.usage.input_tokens, resp.usage.output_tokens, raw=resp.content)


# ── OpenAI-compatible (OpenRouter / Ollama / vLLM / DeepSeek / Qwen / …) ──────

def _openai_tools(tools):
    out = []
    for t in tools or []:
        if t.get("server_tool"):
            continue   # server-side tools (web_search) are Anthropic-only
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}})
    return out


def _openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        if "raw" in m:                       # assistant passthrough (dict we built below)
            out.append(m["raw"])
        elif m["role"] == "tool":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        elif m["role"] == "user":
            imgs = m.get("images", [])
            if imgs:
                parts = [{"type": "text", "text": m.get("text", "")}]
                for img in imgs:
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{img['media_type']};base64,{img['data_b64']}"}})
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": "user", "content": m.get("text", "")})
        else:                                # neutral assistant (seeded history)
            out.append({"role": "assistant", "content": m.get("text", "")})
    return out


async def _chat_openai(feature, system, messages, tools, max_tokens):
    from openai import AsyncOpenAI
    _kind, model, base_url, api_key = _resolve_feature(feature)
    client = AsyncOpenAI(base_url=base_url or None, api_key=api_key or "none")
    otools = _openai_tools(tools)
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=_openai_messages(system, messages))
    if otools:
        kwargs["tools"] = otools
    resp = await client.chat.completions.create(**kwargs)

    msg = resp.choices[0].message
    text = (msg.content or "").strip()
    tool_calls, raw_calls = [], []
    for tc in (msg.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(tc.id, tc.function.name, args))
        raw_calls.append({"id": tc.id, "type": "function",
                          "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
    # Rebuild the assistant turn as a plain dict so it can be appended verbatim.
    raw = {"role": "assistant", "content": msg.content or ""}
    if raw_calls:
        raw["tool_calls"] = raw_calls
    usage = resp.usage
    return LLMResponse(text, tool_calls, "tool_use" if tool_calls else "end",
                       getattr(usage, "prompt_tokens", 0) or 0,
                       getattr(usage, "completion_tokens", 0) or 0, raw=raw)
