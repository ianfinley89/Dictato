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
