import httpx
import json
import re
from typing import Optional
from app.config import USDA_API_KEY
from app.database import get_conn
from app.services import food_sources
from app.services.fatsecret import search_fatsecret
from app.services.nutrition_guard import sanitize_per_100g, KCAL_PER_KJ
from app.services.portion import parse_usda_portions

_AUTHORED_SQL = food_sources.sql_list(food_sources.AUTHORED)
_INVENTED_SQL = food_sources.sql_list(food_sources.INVENTED)

USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
OFF_BASE = "https://world.openfoodfacts.org/cgi/search.pl"

# USDA's nutrient IDs — unique, stable, and the primary key we match on. Names
# are ambiguous: FOUR distinct nutrients are called "Energy".
#
#   1008  Energy                            kcal   <- what we want
#   1062  Energy                            kJ     <- same name, 4.184x the number
#   2048  Energy (Atwater Specific Factors) kcal   <- USDA's per-food coefficients
#   2047  Energy (Atwater General Factors)  kcal   <- the flat 4/4/9
#
# ORDER MATTERS in every tuple below: the first one present wins. Specific
# factors beat general — raw peanuts are 551 kcal/100g specific, 588 general.
_ENERGY_IDS = (1008, 2048, 2047)
_PROTEIN_IDS = (1003,)
_CARB_IDS = (1005,)                   # "by difference", not 1050 "by summation"
_FAT_IDS = (1004,)
_FIBER_IDS = (1079,)

# Fallback for payloads carrying no ids. Same declared order.
_ENERGY_NAMES = ("Energy", "Energy (Atwater Specific Factors)",
                 "Energy (Atwater General Factors)")
_PROTEIN_NAMES = ("Protein",)
_CARB_NAMES = ("Carbohydrate, by difference",)
_FAT_NAMES = ("Total lipid (fat)",)
_FIBER_NAMES = ("Fiber, total dietary",)

# Serving-size units → grams (ml treated as ~1 g/ml, fine for beverages).
_SERVING_UNIT_G = {"g": 1, "grm": 1, "gram": 1, "grams": 1,
                   "ml": 1, "mlt": 1, "milliliter": 1,
                   "oz": 28.35, "onz": 28.35, "ounce": 28.35}


def _serving_grams(size, unit) -> Optional[float]:
    if not size:
        return None
    factor = _SERVING_UNIT_G.get(str(unit or "").lower())
    return round(float(size) * factor, 1) if factor else None


# What each `source` means — privacy, authority, editability, whether its
# numbers are per-100g — is declared once in food_sources. It used to be six
# tuples in five modules with nothing tying them together, which is how
# `estimate` came to inherit the PRECEDENCE of a user's own recipe from a tuple
# that only meant to group them by PRIVACY.


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
        # Try each source in order, but keep going while the results don't
        # actually match what was asked for. Stopping at the first NON-EMPTY
        # source made OFF and FatSecret unreachable from manual search: USDA
        # answers nearly anything with something, so "Kodiak protein pancakes"
        # returned Chinese Pancake and the real Kodiak products (in FatSecret)
        # were never queried. The extra calls only happen when the earlier
        # source missed, so the USDA hourly cap is not burned on easy lookups.
        results = local          # only survives if every source comes back empty
        # Start below zero so the FIRST source that returns anything wins ties
        # (preserving the old ordering), while a later source still takes over
        # when it genuinely covers more of the query. Seeding this from the weak
        # local cache would let stale rows block a proper lookup.
        best = (-1.0, -1.0)
        for fetch in (_search_usda, _search_off, search_fatsecret):
            items = await fetch(query, limit)
            if not items:
                continue
            cached = _cache_all(items)
            score = source_score(cached, q)
            if score > best:
                results, best = cached, score
            if best[0] >= _GOOD_ENOUGH:
                break

    # Brand fallback: if a brand was named but no result matches it, try FatSecret
    # (rich in branded/restaurant items) before the app falls back to the AI lookup.
    if brand and not _has_brand(results, brand):
        fs = _cache_all(await search_fatsecret(f"{query} {brand}", limit))
        matches = [f for f in fs if _has_brand([f], brand)]
        if matches:
            results = matches + [f for f in results if f["id"] not in {m["id"] for m in matches}]
    # Surface the closest matches first — a source can return the right product
    # buried under near-misses.
    return _rank_by_relevance(results, q)[:limit]


def _is_user_food(food: dict, user_id: int) -> bool:
    return food_sources.is_authored_by(food, user_id)


_SCOPE_SQL = f"""
    AND (source NOT IN ({food_sources.sql_list(food_sources.PRIVATE)})
         OR created_by_user_id = ?)
    AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
"""


def _fts_query(query: str) -> str | None:
    """Turn a search phrase into an FTS5 MATCH expression.

    Words are OR'd and each is a prefix term, so "kodiak protein pancake" also
    finds "Kodiak Cakes … pancakes". bm25 then ranks rows matching more words
    higher, which is what `LIKE %whole phrase%` could never do."""
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    terms = [f'"{w}"*' for w in words if len(w) > 1]
    return " OR ".join(terms) if terms else None


def _search_local(query: str, user_id: int, limit: int) -> list[dict]:
    """Public foods (USDA/OFF cache) plus *this* user's own foods. Another user's
    recipes/custom foods are never returned. The user's foods sort first."""
    q = query.strip().lower()
    match = _fts_query(q)
    with get_conn() as conn:
        rows = []
        if match:
            try:
                rows = conn.execute(
                    f"""SELECT f.* FROM foods_fts
                        JOIN foods f ON f.id = foods_fts.rowid
                        WHERE foods_fts MATCH ? {_SCOPE_SQL}
                        ORDER BY
                          CASE WHEN f.source IN ({_AUTHORED_SQL})
                                    AND f.created_by_user_id = ? THEN 0
                               WHEN f.source IN ({_INVENTED_SQL}) THEN 2
                               ELSE 1 END,
                          CASE WHEN lower(f.name) = ? THEN 0
                               WHEN lower(f.name) LIKE ? THEN 1
                               ELSE 2 END,
                          bm25(foods_fts, 10.0, 5.0),
                          length(f.name)
                        LIMIT ?""",
                    (match, user_id, user_id, q, f"{q}%", limit),
                ).fetchall()
            except Exception:
                rows = []      # index missing/corrupt — fall back to LIKE below
        if not rows:
            rows = conn.execute(
                f"""SELECT * FROM foods
                    WHERE name LIKE ? {_SCOPE_SQL}
                    ORDER BY
                      CASE WHEN source IN ({_AUTHORED_SQL})
                                AND created_by_user_id = ? THEN 0
                           WHEN source IN ({_INVENTED_SQL}) THEN 2
                           ELSE 1 END,
                      CASE WHEN lower(name) = ?    THEN 0
                           WHEN lower(name) LIKE ? THEN 1
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
    has *this* food, not just something containing the word.

    An AI estimate never counts. It exists because the databases had nothing at
    the time, so letting it satisfy this test means never going back to ask —
    and the databases keep improving underneath it."""
    return any(_noun_match(_lead_noun(f["name"]), q) for f in foods
               if f.get("source") not in food_sources.INVENTED)


# Words too common in food names to prove a result is relevant. "Kodiak protein
# pancakes" must be judged on "kodiak", not on "pancakes" — USDA answered that
# query with "Chinese Pancake" and we accepted it because the list wasn't empty.
_WEAK_TOKENS = {
    "the", "and", "with", "of", "a", "an", "in", "or", "no", "raw", "cooked",
    "fresh", "frozen", "plain", "original", "classic", "natural", "style",
    "mix", "flavor", "flavour", "protein", "light", "low", "free", "whole",
    "food", "foods", "brand", "made", "from", "large", "small", "medium",
}
_MIN_TOKEN_LEN = 3
# Stop querying further sources once this share of the query's words is covered.
# 1.0 = every distinctive word found; anything less means something was missed.
_GOOD_ENOUGH = 1.0


def _query_tokens(q: str) -> list[str]:
    """The words that actually identify what was asked for."""
    words = re.findall(r"[a-z0-9]+", (q or "").lower())
    return [w for w in words if len(w) >= _MIN_TOKEN_LEN and w not in _WEAK_TOKENS]


def _covers(hay: str, token: str) -> bool:
    """Does this query word appear in the text, allowing for an English plural?

    Stripping only a trailing "s" turns "potatoes" into "potatoe", which matches
    nothing — so "mashed potatoes" scored ONE hit against "Potato, mashed, NFS"
    and two against a branded "MASHED POTATOES", and the generic row lost on hits
    before any tiebreak could reach it. `-es` plurals need both letters off:
    potatoes/tomatoes → potato/tomato, boxes → box, dishes → dish.
    `_noun_match` already knew this; this did not."""
    if token in hay:
        return True
    if token.endswith("es") and token[:-2] in hay:
        return True
    return token.endswith("s") and token[:-1] in hay


def _brand_names_token(brand: str, token: str) -> bool:
    """Whole-word match against a brand. Substring matching is what makes
    "eggs" find Eggland's and "pepper" find Dr Pepper's peppercorns."""
    words = set(re.findall(r"[a-z0-9]+", (brand or "").lower()))
    return token in words or token.rstrip("s") in words or f"{token}s" in words


def _brand_coverage(foods: list[dict], tokens: list[str]) -> float:
    """How much of the query is matched by a result's BRAND.

    Breaks the tie that kept the wrong source: for "Kodiak protein pancakes",
    USDA covers "pancakes" (generic dessert rows) and FatSecret covers "kodiak"
    (brand "Kodiak Cakes") — both 0.5. The brand is what the user actually named,
    so brand coverage decides."""
    if not tokens or not foods:
        return 0.0
    hay = " ".join(f.get("brand") or "" for f in foods).lower()
    return sum(1 for t in tokens if _covers(hay, t)) / len(tokens)


def source_score(foods: list[dict], query: str) -> tuple:
    """(token coverage, brand coverage) — compared lexicographically."""
    tokens = _query_tokens(query)
    return (relevance_score(foods, query), _brand_coverage(foods, tokens))


def relevance_score(foods: list[dict], query: str) -> float:
    """Fraction of the QUERY'S words that appear somewhere in the results.

    Deliberately measured across the result SET, not per result. Searching
    "Kodiak protein pancakes" returned four generic USDA pancakes: judged per
    result they all look relevant (they do say "pancake"), but the word that
    identifies the product — "kodiak" — appears nowhere, and that is the signal
    that this source did not find it. Stopping at the first NON-EMPTY source is
    what made Open Food Facts and FatSecret unreachable, and FatSecret held the
    real Kodiak products."""
    if not foods:
        return 0.0
    tokens = _query_tokens(query)
    if not tokens:
        return 1.0
    hay = " ".join(f"{f.get('name') or ''} {f.get('brand') or ''}" for f in foods).lower()
    return sum(1 for t in tokens if _covers(hay, t)) / len(tokens)


def _source_rank(food: dict) -> int:
    """Who to believe when two rows match the query equally well: a food the user
    built, then any real database, then — only if nothing else fits — the model's
    own guess. Ranked BELOW reference data, not above it, which is the whole
    point of labelling it an estimate."""
    return food_sources.trust(food.get("source"))


def _rank_by_relevance(foods: list[dict], query: str) -> list[dict]:
    """Stable sort putting results that match more of the query first.

    Ties are broken AGAINST branded products when the user never named a brand.
    Packaged goods are titled exactly as people speak — "OATMEAL", "WHITE RICE",
    "SCRAMBLED EGGS" — so they matched every word and won the shortest-name
    tiebreak against "Oatmeal, NFS". That is the wrong food twice over: a
    packaged product quotes its per-100g AS SOLD, which for anything you cook is
    the DRY weight. Someone logging a bowl of oatmeal was getting 377 kcal/100g
    against a real 71, and dry spaghetti at 357 against a cooked 158.

    The penalty sits after `hits`, so naming a product still wins outright — the
    brand's own words ("kodiak", "oikos") appear in no generic row, so it takes
    the comparison before this ever applies."""
    tokens = _query_tokens(query)
    if not tokens:
        return foods
    # Did the user name a brand? A query word that some BRAND carries and no
    # generic row's name uses — "chobani", "kodiak", "oikos". Testing brands
    # alone is far too loose: "whole" is in Whole Foods, "black" in Black Rifle
    # Coffee, and "egg" inside Eggland's, so nearly every generic query looked
    # like it had named a brand and nothing was ever demoted.
    generic_words: set[str] = set()
    for f in foods:
        if not f.get("brand"):
            generic_words |= set(re.findall(r"[a-z0-9]+", (f.get("name") or "").lower()))
    named_brand = any(
        _brand_names_token(f.get("brand") or "", t)
        for t in tokens if not _covers(" ".join(generic_words), t)
        for f in foods
    )

    # English is head-FINAL ("white RICE"), USDA is head-initial ("Rice, white,
    # cooked"). Matching the two heads says the row is that food rather than a
    # dish containing it, which shortest-name alone gets backwards: it preferred
    # "Spaghetti Sauce" to "Spaghetti, cooked" and "Beans And White Rice" to
    # plain rice, purely on length.
    head = tokens[-1]

    def score(f: dict) -> tuple:
        name = f.get("name") or ""
        hay = f"{name} {f.get('brand') or ''}".lower()
        # _covers, not `in`: "eggs" must find "Egg, whole, cooked, scrambled",
        # or the generic row loses on hits before the tiebreak is even reached.
        hits = sum(1 for t in tokens if _covers(hay, t))
        branded = 1 if (f.get("brand") and not named_brand) else 0
        off_head = 0 if _noun_match(_lead_noun(name), head) else 1
        return (-hits, _source_rank(f), branded, off_head, len(name))

    return sorted(foods, key=score)


def _generic_rank(item: dict) -> tuple:
    """Among same-noun generic foods, prefer the plain 'raw' whole food, then the
    least-adorned (shortest) name: 'Spinach, raw' over 'Spinach, creamed'."""
    desc = (item.get("description") or "").lower()
    return (0 if "raw" in desc else 1, len(desc))


def _merge_usda(generic: list[dict], relevance: list[dict], query: str, pool: int) -> list[dict]:
    """Closely-matching generic whole foods first (so 'tomato' → 'Tomatoes, raw'),
    then USDA's relevance order (so 'dr pepper' still yields the brand). Deduped.

    `pool` is a CANDIDATE budget, not the number of results. USDA's relevance
    order leads with packaged products, so cutting to the caller's limit here
    discarded every generic row before `_rank_by_relevance` could weigh it — the
    wider generic fetch above was being thrown away unread. Searching "mashed
    potatoes" kept five branded boxes of dry flakes and dropped "Potato, mashed,
    NFS" entirely; no amount of reordering can recover a row that is gone."""
    q = query.strip().lower()
    strong = sorted((f for f in generic if _is_strong_generic(f, q)), key=_generic_rank)
    merged, seen = [], set()
    for f in strong + relevance + generic:
        key = f.get("fdcId") or f.get("description")
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)
    return merged[:pool]


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
    # Hand the ranker a real pool. search_foods cuts to `limit` after ranking,
    # and everything here is cached either way — the cache growing is the point.
    foods = _merge_usda(generic, relevance, query, max(limit * 3, 24))
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
    """Read a USDA food by nutrient ID, falling back to names.

    Two production bugs came from matching on `nutrientName`, and they were the
    same bug twice: "Energy" is FOUR different nutrients (1008 kcal, 1062 kJ,
    2047/2048 the Atwater variants, all kcal). Keyed by name, a raw carrot took
    the kJ row and cached at 173 kcal/100g, and Foundation foods took whichever
    Atwater row string-hashing surfaced first, so peanuts cached at 551 or 588
    depending on the process. Patching those separately (read the unit, order the
    name tuple) treated symptoms; the IDs are unique and stable, so matching on
    them makes the whole class impossible.

    Names remain the fallback: they are all some payloads carry, and every test
    fixture written before this used them."""
    by_id: dict[int, float] = {}
    by_name: dict[str, float] = {}
    for n in item.get("foodNutrients", []):
        value = n.get("value")
        nid = n.get("nutrientId")
        if nid is not None:
            try:
                by_id.setdefault(int(nid), value)
            except (TypeError, ValueError):
                pass
        name = n.get("nutrientName")
        if name:
            unit = str(n.get("unitName") or "").upper()
            # The name path cannot tell 1008 from 1062, so it still needs the unit.
            if name in _ENERGY_NAMES and unit and unit != "KCAL":
                continue
            by_name.setdefault(name, value)

    def pick(ids: tuple, names: tuple, default: float = 0.0) -> float:
        """First declared id present wins; then first declared name."""
        for i in ids:
            if i in by_id:
                try:
                    return float(by_id[i])
                except (TypeError, ValueError):
                    pass
        for name in names:
            if name in by_name:
                try:
                    return float(by_name[name])
                except (TypeError, ValueError):
                    pass
        return default

    nutrients = {
        "calories": pick(_ENERGY_IDS, _ENERGY_NAMES),
        "protein_g": pick(_PROTEIN_IDS, _PROTEIN_NAMES),
        "carbs_g": pick(_CARB_IDS, _CARB_NAMES),
        "fat_g": pick(_FAT_IDS, _FAT_NAMES),
        "fiber_g": pick(_FIBER_IDS, _FIBER_NAMES),
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


def _off_kcal(n: dict) -> float:
    """Open Food Facts energy: prefer the kcal field; its `energy_100g` /
    `energy-kj_100g` fallbacks are in KILOJOULES, so convert them (this is the
    source of the '1000 cal ground beef' bug)."""
    kcal = n.get("energy-kcal_100g")
    if kcal not in (None, ""):
        return float(kcal or 0)
    kj = n.get("energy-kj_100g", n.get("energy_100g"))
    return round(float(kj or 0) / KCAL_PER_KJ, 1) if kj not in (None, "") else 0.0


# The four fields that make an Open Food Facts row usable as a food.
_OFF_NUTRIENT_KEYS = ("energy-kcal_100g", "energy-kj_100g", "energy_100g",
                      "proteins_100g", "carbohydrates_100g", "fat_100g")


def _off_has_nutrition(n: dict) -> bool:
    """Does this row carry ANY measured nutrition?

    Not the same question as "is it zero calories". Open Food Facts contains
    non-food submissions (a 9V battery was sitting in our cache) whose nutrient
    fields are simply ABSENT, while a genuine zero-calorie product states its
    zeros explicitly — Coke Zero returns all four keys with values. Reading a
    missing key as 0.0 erases that difference, which is how the battery got in.
    """
    return any(n.get(k) not in (None, "") for k in _OFF_NUTRIENT_KEYS)


def _parse_off(item: dict) -> Optional[dict]:
    name = item.get("product_name", "").strip()
    if not name:
        return None
    n = item.get("nutriments", {})
    if not _off_has_nutrition(n):
        return None          # no nutrition data at all — not loggable, food or not
    nutrients = {
        "calories": _off_kcal(n),
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
        # Catch corrupt nutrition (kJ-as-kcal, impossible energy) before storing.
        # Only USDA/OFF carry genuine per-100g energy density; FatSecret encodes
        # per-serving values in the per-100g slot, so it must not be "corrected".
        nutrients_json = food["nutrients_json"]
        if food.get("source") in food_sources.PER_100G:
            try:
                clean, _ = sanitize_per_100g(json.loads(nutrients_json))
                nutrients_json = json.dumps(clean)
            except (TypeError, ValueError):
                pass
        # Household measures come from the locally imported USDA datasets, so a
        # food arrives anchored instead of waiting for the first log that needs a
        # measure to go fetch one.
        portions_json = None
        if food.get("source") == "usda" and food.get("source_id"):
            row = conn.execute("SELECT portions_json FROM usda_portions WHERE fdc_id=?",
                               (str(food["source_id"]),)).fetchone()
            if row:
                portions_json = row["portions_json"]
        cur = conn.execute(
            """INSERT INTO foods (source, source_id, name, brand, serving_desc, serving_g,
                                  nutrients_json, expires_at, portions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                food["source"],
                food.get("source_id"),
                food["name"],
                food.get("brand"),
                food.get("serving_desc"),
                food.get("serving_g"),
                nutrients_json,
                food.get("expires_at"),
                portions_json,
            ),
        )
        return cur.lastrowid


def get_food_by_id(food_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM foods WHERE id=?", (food_id,)).fetchone()
    return _row_to_food(row) if row else None


def _row_to_food(row) -> dict:
    nutrients = json.loads(row["nutrients_json"])
    portions_raw = row["portions_json"] if "portions_json" in row.keys() else None
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
        # None = portions never fetched; [] = fetched, USDA has none.
        "portions": json.loads(portions_raw) if portions_raw else None,
    }


async def backfill_portions(limit: int = 25) -> int:
    """Fetch USDA household measures for foods people have actually logged.

    `ensure_portions` only runs when something needs a measure right then, so in
    practice almost nothing had them: 6% of logged foods, which left half of them
    with no serving anchor at all — no gram weight for a count, nothing to bound a
    wild guess, and a portion picker reduced to "half of this / double this".
    Measured on the live cache, fetching these rescues 14 of 25.

    Runs off the logging path (hourly, small batches) because it is a network call
    per food and the USDA key is capped around 1000/hour. Returns how many gained
    portions."""
    if not USDA_API_KEY:
        return 0
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT f.id FROM foods f
               WHERE f.source = 'usda' AND f.source_id IS NOT NULL
                 AND f.portions_json IS NULL
                 AND (f.id IN (SELECT food_id FROM log_entries)
                      OR f.id IN (SELECT food_id FROM favorites))
               LIMIT ?""",
            (limit,),
        ).fetchall()
    done = 0
    for row in rows:
        food = get_food_by_id(row["id"])
        if not food:
            continue
        before = food.get("portions")
        food = await ensure_portions(food)
        if food.get("portions") is not None and before is None:
            done += 1
    return done


def _norm_name(s: str) -> str:
    """Loose name key: lowercase, punctuation-free, plural-tolerant."""
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w
             for w in re.findall(r"[a-z0-9]+", (s or "").lower())]
    return " ".join(words)


async def strong_db_match(name: str, user_id: int) -> Optional[dict]:
    """A database food that IS the thing being described — the same dish by name,
    not merely a related one. Used to enforce hard rule #1 in code: the agent may
    not invent nutrition for a dish the database already knows.

    Deliberately strict (exact normalized name, or the candidate's lead noun) so
    a genuinely new BRANDED item ("oberto beef jerky" vs generic "Beef Jerky")
    still gets created."""
    key = _norm_name(name)
    if not key:
        return None
    try:
        for f in await search_foods(name, user_id, limit=6):
            if f.get("source") in food_sources.PRIVATE:
                continue          # private/AI rows aren't authority over a new one
            if key in (_norm_name(f["name"]), _norm_name(_lead_noun(f["name"]))):
                return f
    except Exception:
        return None
    return None


async def ensure_portions(food: dict) -> dict:
    """Lazily fetch USDA foodPortions (household gram weights) for a usda food
    the first time a household-measure log needs them; cached forever in
    foods.portions_json. Non-USDA sources have no portion endpoint — no-op."""
    if (food.get("portions") is not None or food.get("source") != "usda"
            or not food.get("source_id")):
        return food
    # The published datasets are imported locally, so most foods are answered
    # without a network call at all (scripts/import_usda_reference.py).
    with get_conn() as conn:
        row = conn.execute("SELECT portions_json FROM usda_portions WHERE fdc_id=?",
                           (str(food["source_id"]),)).fetchone()
        if row:
            conn.execute("UPDATE foods SET portions_json=? WHERE id=?",
                         (row["portions_json"], food["id"]))
            food["portions"] = json.loads(row["portions_json"])
            return food
    if not USDA_API_KEY:
        return food
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{USDA_BASE}/food/{food['source_id']}",
                                 params={"api_key": USDA_API_KEY})
            r.raise_for_status()
            portions = parse_usda_portions(r.json())
    except Exception:
        return food     # leave NULL so a later log retries
    with get_conn() as conn:
        conn.execute("UPDATE foods SET portions_json=? WHERE id=?",
                     (json.dumps(portions), food["id"]))
    food["portions"] = portions
    return food
