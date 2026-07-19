"""Provider-neutral → provider translation for the swappable LLM layer.
Pure functions, no network."""
from app.services import llm


# ── Model selection ───────────────────────────────────────────────────────────

def test_model_for_precedence(monkeypatch):
    monkeypatch.setattr(llm.config, "AGENT_MODEL", "")
    monkeypatch.setattr(llm.config, "COACH_MODEL", "")
    monkeypatch.setattr(llm.config, "LLM_MODEL", "")
    assert llm._model_for("agent") == llm.MODEL_HAIKU        # anthropic default

    monkeypatch.setattr(llm.config, "LLM_MODEL", "deepseek/deepseek-chat")
    assert llm._model_for("agent") == "deepseek/deepseek-chat"   # global default

    monkeypatch.setattr(llm.config, "AGENT_MODEL", "qwen/qwen-2.5-72b")
    assert llm._model_for("agent") == "qwen/qwen-2.5-72b"        # per-feature wins
    assert llm._model_for("coach") == "deepseek/deepseek-chat"   # coach still global


# ── Per-feature routing (_resolve_feature) ────────────────────────────────────

def _clear_routing(monkeypatch):
    for v in ("VOICE_MODEL", "PHOTO_MODEL", "COACH_MODEL", "TRIAGE_MODEL",
              "AGENT_MODEL", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.setattr(llm.config, v, "")
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm.config, "ANTHROPIC_API_KEY", "sk-ant")


def test_resolve_defaults_to_anthropic_haiku(monkeypatch):
    _clear_routing(monkeypatch)
    assert llm._resolve_feature("voice")[:2] == ("anthropic", llm.MODEL_HAIKU)
    assert llm._resolve_feature("coach")[:2] == ("anthropic", llm.MODEL_HAIKU)
    assert llm._resolve_feature("photo")[:2] == ("anthropic", llm.MODEL_HAIKU)


def test_resolve_explicit_openrouter_provider(monkeypatch):
    _clear_routing(monkeypatch)
    monkeypatch.setattr(llm.config, "PHOTO_MODEL", "openrouter:google/gemini-3.1-flash-lite-image")
    monkeypatch.setattr(llm.config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "sk-or-x")
    kind, model, base, key = llm._resolve_feature("photo")
    assert (kind, model, base, key) == (
        "openai", "google/gemini-3.1-flash-lite-image", "https://openrouter.ai/api/v1", "sk-or-x")


def test_resolve_preserves_free_suffix(monkeypatch):
    _clear_routing(monkeypatch)
    monkeypatch.setattr(llm.config, "VOICE_MODEL", "openrouter:deepseek/deepseek-chat:free")
    kind, model, _b, _k = llm._resolve_feature("voice")
    assert kind == "openai" and model == "deepseek/deepseek-chat:free"


def test_resolve_photo_stays_vision_when_text_is_swapped(monkeypatch):
    """Voice on DeepSeek (no vision); photo unset must NOT inherit it — it falls
    back to a vision-capable Anthropic model so photo logging keeps working."""
    _clear_routing(monkeypatch)
    monkeypatch.setattr(llm.config, "VOICE_MODEL", "openrouter:deepseek/deepseek-chat")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "sk-or-x")
    assert llm._resolve_feature("voice")[0] == "openai"
    assert llm._resolve_feature("photo")[:2] == ("anthropic", llm.MODEL_HAIKU)


def test_resolve_local_provider(monkeypatch):
    _clear_routing(monkeypatch)
    monkeypatch.setattr(llm.config, "COACH_MODEL", "local:gemma3")
    monkeypatch.setattr(llm.config, "LOCAL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(llm.config, "LOCAL_API_KEY", "ollama")
    assert llm._resolve_feature("coach") == ("openai", "gemma3", "http://localhost:11434/v1", "ollama")


def test_resolve_bare_model_uses_global_provider(monkeypatch):
    _clear_routing(monkeypatch)
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm.config, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm.config, "LLM_API_KEY", "sk-or-x")
    monkeypatch.setattr(llm.config, "VOICE_MODEL", "deepseek/deepseek-chat")  # no provider prefix
    kind, model, base, key = llm._resolve_feature("voice")
    assert (kind, model, base) == ("openai", "deepseek/deepseek-chat", "https://openrouter.ai/api/v1")


# ── Tool translation ──────────────────────────────────────────────────────────

_TOOLS = [
    {"name": "log_food", "description": "log it", "input_schema": {"type": "object", "properties": {}}},
    {"server_tool": "web_search", "max_uses": 3},
]


def test_anthropic_tools_include_web_search():
    out = llm._anthropic_tools(_TOOLS)
    assert out[0] == {"name": "log_food", "description": "log it", "input_schema": {"type": "object", "properties": {}}}
    assert out[1]["type"] == "web_search_20250305"
    assert out[1]["max_uses"] == 3


def test_openai_tools_drop_server_tools():
    out = llm._openai_tools(_TOOLS)
    assert len(out) == 1                       # web_search dropped
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "log_food"
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ── Message translation ───────────────────────────────────────────────────────

def test_anthropic_messages_user_image_and_tool_result():
    msgs = [
        {"role": "user", "text": "what is this", "images": [{"media_type": "image/png", "data_b64": "AAA"}]},
        {"role": "assistant", "raw": [{"type": "tool_use", "id": "t1"}]},   # passthrough
        {"role": "tool", "results": [{"id": "t1", "name": "log_food", "content": "ok"}]},
    ]
    out = llm._anthropic_messages(msgs)
    assert out[0]["content"][0]["type"] == "image"
    assert out[0]["content"][0]["source"]["data"] == "AAA"
    assert out[0]["content"][1] == {"type": "text", "text": "what is this"}
    assert out[1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}
    assert out[2]["role"] == "user"
    assert out[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}


def test_anthropic_messages_neutral_assistant_history():
    out = llm._anthropic_messages([{"role": "assistant", "text": "hi there"}])
    assert out[0] == {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}


def test_openai_messages_system_image_and_tool():
    msgs = [
        {"role": "user", "text": "what is this", "images": [{"media_type": "image/png", "data_b64": "AAA"}]},
        {"role": "assistant", "raw": {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]}},
        {"role": "tool", "results": [{"id": "t1", "name": "log_food", "content": "ok"}]},
    ]
    out = llm._openai_messages("be helpful", msgs)
    assert out[0] == {"role": "system", "content": "be helpful"}
    parts = out[1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert out[2]["tool_calls"][0]["id"] == "t1"          # assistant raw passthrough
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}


def test_openai_messages_plain_user_and_assistant():
    out = llm._openai_messages("sys", [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi"},
    ])
    assert out[1] == {"role": "user", "content": "hello"}
    assert out[2] == {"role": "assistant", "content": "hi"}
