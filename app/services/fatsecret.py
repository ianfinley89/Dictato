"""FatSecret food search (OAuth2 client-credentials).

License note: results may not be stored locally beyond FATSECRET_TTL_HOURS, so each
cached `foods` row carries an `expires_at`; a cleanup purges expired, unlogged rows.
Foods the user actually logged keep their snapshot (the user's own diary record).
"""
import re
import json
import time
import httpx
from datetime import datetime, timezone, timedelta
from app.config import FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET, FATSECRET_TTL_HOURS

_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
_API_URL = "https://platform.fatsecret.com/rest/server.api"

_token = {"value": None, "exp": 0.0}

# "Per 100g - Calories: 52kcal | Fat: 0.17g | Carbs: 13.81g | Protein: 0.26g"
_DESC_RE = re.compile(
    r"Per\s+(?P<serving>.+?)\s*-\s*Calories:\s*(?P<cal>[\d.]+)kcal\s*\|\s*"
    r"Fat:\s*(?P<fat>[\d.]+)g\s*\|\s*Carbs:\s*(?P<carb>[\d.]+)g\s*\|\s*"
    r"Protein:\s*(?P<prot>[\d.]+)g",
    re.IGNORECASE,
)


def enabled() -> bool:
    return bool(FATSECRET_CLIENT_ID and FATSECRET_CLIENT_SECRET)


async def _access_token() -> str:
    now = time.time()
    if _token["value"] and _token["exp"] - 60 > now:
        return _token["value"]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials", "scope": "basic"},
            auth=(FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET),
        )
    r.raise_for_status()
    j = r.json()
    _token["value"] = j["access_token"]
    _token["exp"] = now + float(j.get("expires_in", 3600))
    return _token["value"]


async def search_fatsecret(query: str, limit: int = 10) -> list[dict]:
    """Return food dicts in the shared shape, each with an `expires_at`."""
    if not enabled():
        return []
    try:
        token = await _access_token()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                _API_URL,
                params={"method": "foods.search", "search_expression": query,
                        "format": "json", "max_results": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code != 200:
            return []
        foods = (r.json() or {}).get("foods") or {}
        items = foods.get("food")
        if not items:
            return []
        if isinstance(items, dict):   # single result → object, not list
            items = [items]
        expires = (datetime.now(timezone.utc) + timedelta(hours=FATSECRET_TTL_HOURS)).isoformat()
        return [f for it in items if (f := _parse(it, expires))]
    except Exception:
        return []


def _parse(item: dict, expires_at: str):
    m = _DESC_RE.search(item.get("food_description") or "")
    if not m:
        return None
    serving = m.group("serving").strip()
    nutrients = {
        "calories": float(m.group("cal")),
        "protein_g": float(m.group("prot")),
        "carbs_g": float(m.group("carb")),
        "fat_g": float(m.group("fat")),
        "fiber_g": 0.0,
        "micros": {},
    }
    # food_description macros are per the stated serving. If it's "100 g", they're
    # already per-100g; otherwise treat as one nominal serving (serving_g = 100),
    # matching how custom foods store per-serving macros.
    per_100g = bool(re.search(r"\b100\s*g\b", serving, re.IGNORECASE))
    return {
        "source": "fatsecret",
        "source_id": str(item.get("food_id") or ""),
        "name": (item.get("food_name") or "").strip(),
        "brand": (item.get("brand_name") or "").strip() or None,
        "serving_desc": "100 g" if per_100g else serving,
        "serving_g": None if per_100g else 100.0,
        "nutrients_json": json.dumps(nutrients),
        "expires_at": expires_at,
    }
