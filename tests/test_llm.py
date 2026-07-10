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
