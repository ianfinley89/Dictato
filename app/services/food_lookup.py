import httpx
import json
from typing import Optional
from app.config import USDA_API_KEY
from app.database import get_conn
from app.services.fatsecret import search_fatsecret

USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
OFF_BASE = "https://world.openfoodfacts.org/cgi/search.pl"

_ENERGY_NAMES = {"Energy", "Energy (Atwater General Factors)", "Energy (Atwater Specific Factors)"}
_PROTEIN_NAMES = {"Protein"}
_CARB_NAMES = {"Carbohydrate, by difference"}
_FAT_NAMES = {"Total lipid (fat)"}
_FIBER_NAMES = {"Fiber, total dietary"}

# Serving-size units → grams (ml treated as ~1 g/ml, fine for beverages).
_SERVING_UNIT_G = {"g": 1, "grm": 1, "gram": 1, "grams": 1,
                   "ml": 1, "mlt": 1, "milliliter": 1,
                   "oz": 28.35, "onz": 28.35, "ounce": 28.35}


def _serving_grams(size, unit) -> Optional[float]:
    if not size:
        return None
    factor = _SERVING_UNIT_G.get(str(unit or "").lower())
    return round(float(size) * factor, 1) if factor else None


_USER_SOURCES = ("user", "recipe", "estimate")


def _cache_all(items: list[dict]) -> list[dict]:
    return [f for i in items if (f := get_food_by_id(_cache_food(i)))]


def _has_brand(foods: list[dict], brand: str) -> bool:
    b = brand.lower()
    return any(b in f"{f['name']} {f.get('brand') or ''}".lower() for f in foods)


async def search_foods(query: str, user_id: int, limit: int = 10, brand: str | None = None) -> list[dict]:
    q = query.strip().lower()
    local = _search_local(query, user_id, limit)
    # The user's own foods always win; otherwise trust the cache only on a strong
    # lead-noun match so a stray cached composite can't mask 'tomato'.
    has_user_food = any(_is_user_food(f, user_id) for f in local)
    if local and (has_user_food or _has_strong_local(local, q)):
        results = local
    else:
        results = local  # fallback if every source is empty
        # FatSecret runs last — a fallback before the AI pass.
        for fetch in (_search_usda, _search_off, search_fatsecret):
            items = await fetch(query, limit)
            if items:
                results = _cache_all(items)
                break

    # Brand fallback: if a brand was named but no result matches it, try FatSecret
    # (rich in branded/restaurant items) before the app falls back to the AI lookup.
    if brand and not _has_brand(results, brand):
        fs = _cache_all(await search_fatsecret(f"{query} {brand}", limit))
        matches = [f for f in fs if _has_brand([f], brand)]
        if matches:
            results = matches + [f for f in results if f["id"] not in {m["id"] for m in matches}]
    return results


def _is_user_food(food: dict, user_id: int) -> bool:
    return food.get("source") in _USER_SOURCES and food.get("created_by_user_id") == user_id


def _search_local(query: str, user_id: int, limit: int) -> list[dict]:
    """Public foods (USDA/OFF cache) plus *this* user's own foods. Another user's
    recipes/custom foods are never returned. The user's foods sort first."""
    q = query.strip().lower()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM foods
               WHERE name LIKE ?
                 AND (source NOT IN ('user', 'recipe', 'estimate') OR created_by_user_id = ?)
                 AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))  -- skip expired licensed cache
               ORDER BY
                 CASE WHEN created_by_user_id = ? THEN 0 ELSE 1 END,   -- your foods first
                 CASE WHEN lower(name) = ?        THEN 0
                      WHEN lower(name) LIKE ?     THEN 1
                      ELSE 2 END,
                 length(name)
               LIMIT ?""",
            (f"%{query}%", user_id, user_id, q, f"{q}%", limit),
        ).fetchall()
    return [_row_to_food(r) for r in rows]


# USDA's generic / whole-food datasets (as opposed to "Branded" packaged products).
_GENERIC_TYPES = {"Foundation", "SR Legacy", "Survey (FNDDS)"}


def _noun_match(seg: str, q: str) -> bool:
    """Lead noun equals the query, with simple plural tolerance (tomato↔tomatoes)."""
    return seg == q or seg == q + "s" or seg == q + "es" or q == seg + "s" or q == seg + "es"


def _lead_noun(name: str) -> str:
    return (name or "").split(",")[0].strip().lower()


def _is_strong_generic(item: dict, q: str) -> bool:
    """A generic whole-food entry whose lead noun matches the query, e.g.
    'Tomatoes, raw' for 'tomato'. Surfaces real ingredients above branded
    look-alikes ('TOMATO'), without grabbing composite dishes like
    'Cucumber salad made with…' for 'cucumber'."""
    if item.get("dataType") not in _GENERIC_TYPES:
        return False
    return _noun_match(_lead_noun(item.get("description")), q)


def _has_strong_local(foods: list[dict], q: str) -> bool:
    """True if a cached food's lead noun matches the query — i.e. the cache really
    has *this* food, not just something containing the word."""
    return any(_noun_match(_lead_noun(f["name"]), q) for f in foods)


def _generic_rank(item: dict) -> tuple:
    """Among same-noun generic foods, prefer the plain 'raw' whole food, then the
    least-adorned (shortest) name: 'Spinach, raw' over 'Spinach, creamed'."""
    desc = (item.get("description") or "").lower()
    return (0 if "raw" in desc else 1, len(desc))


def _merge_usda(generic: list[dict], relevance: list[dict], query: str, limit: int) -> list[dict]:
    """Closely-matching generic whole foods first (so 'tomato' → 'Tomatoes, raw'),
    then USDA's relevance order (so 'dr pepper' still yields the brand). Deduped."""
    q = query.strip().lower()
    strong = sorted((f for f in generic if _is_strong_generic(f, q)), key=_generic_rank)
    merged, seen = [], set()
    for f in strong + relevance + generic:
        key = f.get("fdcId") or f.get("description")
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)
    return merged[:limit]


async def _usda_call(query: str, limit: int, data_type: str | None) -> list[dict]:
    params = {"query": query, "pageSize": limit, "api_key": USDA_API_KEY}
    if data_type:
        params["dataType"] = data_type
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{USDA_BASE}/foods/search", params=params)
        return r.json().get("foods", []) if r.status_code == 200 else []
    except Exception:
        return []


async def _search_usda(query: str, limit: int) -> list[dict]:
    if not USDA_API_KEY:
        return []
    # Fetch a wider generic pool so the plain 'X, raw' entry is present even when
    # composite dishes ('X salad', 'creamed X') rank ahead of it.
    generic = await _usda_call(query, max(limit, 25), "Foundation,SR Legacy,Survey (FNDDS)")
    relevance = await _usda_call(query, limit, None)
    foods = _merge_usda(generic, relevance, query, limit)
    return [p for item in foods if (p := _parse_usda(item))]


async def _search_off(query: str, limit: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                OFF_BASE,
                params={
                    "search_terms": query,
                    "search_simple": 1,
                    "action": "process",
                    "json": 1,
                    "page_size": limit,
                    "fields": "product_name,brands,nutriments,serving_size,serving_quantity",
                },
            )
        if r.status_code != 200:
            return []
        products = r.json().get("products", [])
        return [p for item in products if (p := _parse_off(item))]
    except Exception:
        return []


def _parse_usda(item: dict) -> Optional[dict]:
    raw = {n["nutrientName"]: n.get("value", 0) for n in item.get("foodNutrients", [])}

    def pick(names: set, default: float = 0.0) -> float:
        for name in names:
            if name in raw:
                return float(raw[name])
        return default

    nutrients = {
        "calories": pick(_ENERGY_NAMES),
        "protein_g": pick(_PROTEIN_NAMES),
        "carbs_g": pick(_CARB_NAMES),
        "fat_g": pick(_FAT_NAMES),
        "fiber_g": pick(_FIBER_NAMES),
        "micros": {},
    }
    serving = item.get("servingSize")
    serving_unit = item.get("servingSizeUnit", "g")
    serving_g = _serving_grams(serving, serving_unit)
    household = item.get("householdServingFullText")
    serving_desc = household or (f"{serving} {serving_unit}" if serving else None)
    return {
        "source": "usda",
        "source_id": str(item.get("fdcId", "")),
        "name": item.get("description", "").title(),
        "brand": item.get("brandOwner") or item.get("brandName"),
        "serving_desc": serving_desc,
        "serving_g": serving_g,
        "nutrients_json": json.dumps(nutrients),
    }


def _parse_off(item: dict) -> Optional[dict]:
    name = item.get("product_name", "").strip()
    if not name:
        return None
    n = item.get("nutriments", {})
    nutrients = {
        "calories": float(n.get("energy-kcal_100g", n.get("energy_100g", 0)) or 0),
        "protein_g": float(n.get("proteins_100g", 0) or 0),
        "carbs_g": float(n.get("carbohydrates_100g", 0) or 0),
        "fat_g": float(n.get("fat_100g", 0) or 0),
        "fiber_g": float(n.get("fiber_100g", 0) or 0),
        "micros": {},
    }
    try:
        serving_g = round(float(item["serving_quantity"]), 1) if item.get("serving_quantity") else None
    except (ValueError, TypeError):
        serving_g = None
    return {
        "source": "off",
        "source_id": item.get("id") or item.get("_id"),
        "name": name.title(),
        "brand": item.get("brands"),
        "serving_desc": item.get("serving_size"),
        "serving_g": serving_g,
        "nutrients_json": json.dumps(nutrients),
    }


def _cache_food(food: dict) -> int:
    with get_conn() as conn:
        if food.get("source_id"):
            existing = conn.execute(
                "SELECT id FROM foods WHERE source=? AND source_id=?",
                (food["source"], food["source_id"]),
            ).fetchone()
            if existing:
                # Re-fetched within the window → extend the license TTL.
                if food.get("expires_at"):
                    conn.execute("UPDATE foods SET expires_at=? WHERE id=?",
                                 (food["expires_at"], existing["id"]))
                return existing["id"]
        cur = conn.execute(
            """INSERT INTO foods (source, source_id, name, brand, serving_desc, serving_g, nutrients_json, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                food["source"],
                food.get("source_id"),
                food["name"],
                food.get("brand"),
                food.get("serving_desc"),
                food.get("serving_g"),
                food["nutrients_json"],
                food.get("expires_at"),
            ),
        )
        return cur.lastrowid


def get_food_by_id(food_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM foods WHERE id=?", (food_id,)).fetchone()
    return _row_to_food(row) if row else None


def _row_to_food(row) -> dict:
    nutrients = json.loads(row["nutrients_json"])
    return {
        "id": row["id"],
        "source": row["source"],
        "source_id": row["source_id"],
        "name": row["name"],
        "brand": row["brand"],
        "serving_desc": row["serving_desc"],
        "serving_g": row["serving_g"],
        "created_by_user_id": row["created_by_user_id"],
        "nutrients_per_100g": nutrients,
    }
