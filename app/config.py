import os
from dotenv import load_dotenv

load_dotenv()


def get_db_path() -> str:
    return os.getenv("DATABASE_PATH", "data/dictato.db")


USDA_API_KEY: str = os.getenv("USDA_FOOD_DATA_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-use-a-long-random-string-in-production")
SESSION_COOKIE_NAME: str = "dictato_session"
AI_DAILY_LIMIT: int = int(os.getenv("AI_DAILY_LIMIT", "20"))
SECURE_COOKIES: bool = os.getenv("SECURE_COOKIES", "false").lower() == "true"
VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT: str = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")
FATSECRET_CLIENT_ID: str = os.getenv("FATSECRET_CLIENT_ID", "")
FATSECRET_CLIENT_SECRET: str = os.getenv("FATSECRET_CLIENT_SECRET", "")
# FatSecret license: cached results must be purged after this many hours.
FATSECRET_TTL_HOURS: int = int(os.getenv("FATSECRET_TTL_HOURS", "24"))
# Local speech-to-text (faster-whisper) model size: tiny/base/small/medium.
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "small")

# ── Swappable LLM backend (coach + voice/photo agent) ─────────────────────────
# Default keeps everything on Anthropic (native tool use, vision, web-search).
# Set LLM_PROVIDER=openai to route the agent + coach through any OpenAI-compatible
# endpoint — OpenRouter (DeepSeek/Qwen/Gemini) or a local Ollama/vLLM server (Gemma).
# The nutrition web-lookup always stays on Anthropic (it needs server-side search).
# Comma-separated emails allowed to see the admin usage dashboard.
ADMIN_EMAILS: set[str] = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic")   # 'anthropic' | 'openai'
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")            # e.g. https://openrouter.ai/api/v1  |  http://localhost:11434/v1
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")             # provider key (any value for local servers)
LLM_MODEL: str = os.getenv("LLM_MODEL", "")                 # default model when provider=openai
AGENT_MODEL: str = os.getenv("AGENT_MODEL", "")             # per-feature overrides (optional)
COACH_MODEL: str = os.getenv("COACH_MODEL", "")

# Per-feature model routing. Each value may carry an explicit "provider:model"
# prefix — provider ∈ anthropic | openrouter | local — or a bare model id (which
# uses the global provider above). Unset falls back to the global default;
# PHOTO defaults to Anthropic Haiku so vision never breaks when the text features
# are pointed at a non-vision model like DeepSeek.
VOICE_MODEL: str = os.getenv("VOICE_MODEL", "")             # voice/text logging agent
PHOTO_MODEL: str = os.getenv("PHOTO_MODEL", "")             # photo logging agent (needs vision)
TRIAGE_MODEL: str = os.getenv("TRIAGE_MODEL", "")           # issue triage (trivial — cheapest model is fine)
# Named endpoints the specs above point at:
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
LOCAL_BASE_URL: str = os.getenv("LOCAL_BASE_URL", "")       # e.g. http://localhost:11434/v1 (Ollama/vLLM)
LOCAL_API_KEY: str = os.getenv("LOCAL_API_KEY", "ollama")
