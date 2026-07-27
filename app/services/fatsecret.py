"""FatSecret food search (OAuth2 client-credentials).

License note: results may not be stored locally beyond FATSECRET_TTL_HOURS, so each
cached `foods` row carries an `expires_at`; a cleanup purges expired, unlogged rows.
Foods the user actually logged keep their snapshot (the user's own diary record).
"""
import logging
import re
import json
import time
import httpx
from datetime import datetime, timezone, timedelta
from app.config import FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET, FATSECRET_TTL_HOURS

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
_API_URL = "https://platform.fatsecret.com/rest/server.api"

_token = {"value": None, "exp": 0.0}

# Failures here must never break a lookup (we just fall through to the next
# source) — but they must not be INVISIBLE either. Silently returning [] hid a
# config error (FatSecret error 21: this host's IP is not allow-listed in their
# dev console) that disabled the whole source for weeks. Surfaced in the admin
# pane via health().
_health = {"ok": None, "at": None, "message": None}


def _note_failure(message: str) -> None:
    _health.update({"ok": False, "message": message[:300],
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    log.warning("FatSecret lookup unavailable: %s", message[:300])


def _note_ok() -> None:
    _health.update({"ok": True, "message": None,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def health() -> dict:
    """Last known state of this integration, for the admin pane."""
    return {"enabled": enabled(), **_health}

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
            _note_failure(f"HTTP {r.status_code}")
            return []
        payload = r.json() or {}
        # FatSecret reports auth/IP problems as HTTP 200 with an error body.
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            _note_failure(f"error {err.get('code')}: {err.get('message')}")
            return []
        _note_ok()
        foods = payload.get("foods") or {}
        items = foods.get("food")
        if not items:
            return []
        if isinstance(items, dict):   # single result → object, not list
            items = [items]
        expires = (datetime.now(timezone.utc) + timedelta(hours=FATSECRET_TTL_HOURS)).isoformat()
        return [f for it in items if (f := _parse(it, expires))]
    except Exception as e:
        _note_failure(repr(e))
        return []


# A gram weight inside the serving text: "429g", "1 cup (240 g)", "100 g".
_SERVING_G_RE = re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.IGNORECASE)
_BARE_GRAMS_RE = re.compile(r"\d+(?:\.\d+)?\s*g", re.IGNORECASE)


def _serving_grams(serving: str) -> float | None:
    """Real weight of the stated serving, when FatSecret gives one."""
    matches = _SERVING_G_RE.findall(serving or "")
    if not matches:
        return None
    try:                       # a parenthesised weight wins: "1 cup (240 g)"
        grams = float(matches[-1])
    except ValueError:
        return None
    return grams if 0 < grams <= 20000 else None


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
    # The macros are per the STATED serving, and that serving is often given in
    # grams — "Per 429g", and for community recipe entries "Per 5454g". Ignoring
    # that weight and filing the numbers under a nominal 100 g produced
    # physically impossible rows: Chicken Pho was stored as 315 g of protein per
    # 100 g. Scaling by the real weight gives 5.8 g/100 g, which is simply
    # correct. Only when no weight is stated do we fall back to the nominal
    # serving used by custom foods.
    grams = _serving_grams(serving)
    if grams:
        factor = 100.0 / grams
        nutrients = {k: (round(v * factor, 2) if isinstance(v, (int, float)) else v)
                     for k, v in nutrients.items()}
        # A bare weight ("100g", "429g") is the basis the numbers are quoted
        # against, not a portion someone eats — leave serving_g unset so nothing
        # treats it as "one serving". A described portion ("1 cup (240 g)") is a
        # real serving and worth keeping.
        bare_basis = bool(_BARE_GRAMS_RE.fullmatch(serving.strip()))
        return {
            "source": "fatsecret",
            "source_id": str(item.get("food_id") or ""),
            "name": (item.get("food_name") or "").strip(),
            "brand": (item.get("brand_name") or "").strip() or None,
            "serving_desc": f"{grams:g} g" if bare_basis else serving,
            "serving_g": None if bare_basis else round(grams, 1),
            "nutrients_json": json.dumps(nutrients),   # now genuinely per 100 g
            "expires_at": expires_at,
        }
    return {
        "source": "fatsecret",
        "source_id": str(item.get("food_id") or ""),
        "name": (item.get("food_name") or "").strip(),
        "brand": (item.get("brand_name") or "").strip() or None,
        "serving_desc": serving,
        "serving_g": 100.0,          # nominal: macros are per one serving
        "nutrients_json": json.dumps(nutrients),
        "expires_at": expires_at,
    }
