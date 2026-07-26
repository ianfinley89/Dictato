import json

from fastapi import APIRouter, Query, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.auth import get_current_user_id
from app.services.food_lookup import search_foods, get_food_by_id, _row_to_food
from app.services import ai
from app.services.ai_usage import check_and_increment
from app.services.nutrition_guard import sanitize_per_100g
from app.services.logging import resync_entry_snapshot
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT
from app.database import get_conn

router = APIRouter(prefix="/api/foods", tags=["foods"])


class WebLookupRequest(BaseModel):
    name: str
    brand: Optional[str] = None


class FoodEdit(BaseModel):
    """Corrections to an AI-created food. Omitted fields are left alone, and
    macros may be given per serving or per 100 g — the server converts, so a
    user reading a package label never has to do the maths."""
    name: Optional[str] = None
    brand: Optional[str] = None
    serving_g: Optional[float] = None
    serving_desc: Optional[str] = None
    values_per: Optional[str] = "100g"
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    # The entry that prompted this correction, so it reflects the fix too.
    resync_entry_id: Optional[int] = None


@router.post("/weblookup")
async def web_lookup(req: WebLookupRequest, request: Request):
    """Assisted draft: search the web for a branded/restaurant item's PUBLISHED
    nutrition. The user verifies before saving (never auto-logged)."""
    uid = get_current_user_id(request)
    if not req.name.strip():
        raise HTTPException(400, "Name required")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "Web lookup is not configured.")
    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT}) reached. Try again tomorrow.")
    try:
        return await ai.lookup_nutrition_web(req.name.strip(), (req.brand or "").strip() or None, uid)
    except Exception:
        raise HTTPException(502, "Web lookup failed. Try again or enter it manually.")


@router.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1), brand: Optional[str] = None):
    uid = get_current_user_id(request)
    return await search_foods(q, uid, brand=(brand or None))


@router.get("/quick")
async def quick_picks(request: Request):
    """One-tap foods for the home screen: starred favorites + recently-logged."""
    uid = get_current_user_id(request)
    with get_conn() as conn:
        fav = conn.execute(
            """SELECT f.* FROM favorites fav JOIN foods f ON f.id = fav.food_id
               WHERE fav.user_id=? ORDER BY fav.created_at DESC LIMIT 20""",
            (uid,),
        ).fetchall()
        fav_ids = {r["id"] for r in fav}
        recent = conn.execute(
            """SELECT f.*, MAX(le.eaten_at) AS last_eaten
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.user_id=?
               GROUP BY le.food_id
               ORDER BY last_eaten DESC LIMIT 12""",
            (uid,),
        ).fetchall()
    return {
        "favorites": [_row_to_food(r) for r in fav],
        "recents": [_row_to_food(r) for r in recent if r["id"] not in fav_ids],
    }


@router.get("/mine")
async def my_foods(request: Request):
    """The user's own custom foods and recipes."""
    uid = get_current_user_id(request)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM foods WHERE created_by_user_id=? AND source IN ('user','recipe') ORDER BY name",
            (uid,),
        ).fetchall()
    return [_row_to_food(r) for r in rows]


@router.post("/{food_id}/favorite")
async def add_favorite(food_id: int, request: Request):
    uid = get_current_user_id(request)
    if not _can_access(food_id, uid):
        raise HTTPException(404, "Food not found")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (user_id, food_id) VALUES (?,?)", (uid, food_id)
        )
    return {"ok": True}


@router.delete("/{food_id}/favorite")
async def remove_favorite(food_id: int, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        conn.execute("DELETE FROM favorites WHERE user_id=? AND food_id=?", (uid, food_id))
    return {"ok": True}


@router.get("/{food_id}")
async def get_food(food_id: int, request: Request):
    uid = get_current_user_id(request)
    if not _can_access(food_id, uid):
        raise HTTPException(404, "Food not found")
    return get_food_by_id(food_id)


@router.put("/{food_id}")
async def edit_food(food_id: int, body: FoodEdit, request: Request):
    """Correct a food the AI created for you — its name, brand, or nutrition.

    A voice capture heard "Kodiak" as "Kodak", and the resulting row was not only
    wrong for that log but cached under the wrong name for every future one.
    Only rows this user created and only AI-made sources ('web'/'estimate') are
    editable: a USDA or Open Food Facts row is shared reference data and must not
    be rewritten by one user, and existing log entries keep their own snapshots
    either way (history stays stable, per the data model)."""
    uid = get_current_user_id(request)
    food = get_food_by_id(food_id)
    if not food or not _can_access(food_id, uid):
        raise HTTPException(404, "Food not found")
    if food["source"] not in ("web", "estimate", "user"):
        raise HTTPException(
            403, f"{food['source'].upper()} foods are shared reference data and "
                 f"can't be edited. Create your own version instead.")
    if food.get("created_by_user_id") != uid:
        raise HTTPException(403, "You can only edit foods you created.")

    n = dict(food["nutrients_per_100g"])
    per = (body.values_per or "100g").lower()
    if per == "serving":
        serving_g = body.serving_g or food.get("serving_g")
        if not serving_g:
            raise HTTPException(400, "serving_g is required when values_per='serving'")
        factor = 100.0 / float(serving_g)
    else:
        factor = 1.0
    for field, cap in (("calories", 2000), ("protein_g", 100), ("carbs_g", 100),
                       ("fat_g", 100), ("fiber_g", 80)):
        v = getattr(body, field, None)
        if v is not None:
            n[field] = max(0.0, min(cap, float(v) * factor))
    # Same plausibility guard every other per-100g write goes through.
    n, _ = sanitize_per_100g(n)

    name = (body.name or food["name"]).strip().lower()[:120]
    if not name:
        raise HTTPException(400, "name cannot be empty")
    brand = body.brand.strip().lower()[:80] if body.brand is not None else food.get("brand")
    serving_g = body.serving_g if body.serving_g is not None else food.get("serving_g")
    serving_desc = (body.serving_desc if body.serving_desc is not None
                    else food.get("serving_desc"))

    with get_conn() as conn:
        conn.execute(
            """UPDATE foods SET name=?, brand=?, serving_g=?, serving_desc=?,
               nutrients_json=? WHERE id=?""",
            (name, brand or None, serving_g, serving_desc, json.dumps(n), food_id))
    updated = get_food_by_id(food_id)
    if body.resync_entry_id:
        entry = resync_entry_snapshot(uid, body.resync_entry_id)
        if entry:
            updated = {**updated, "resynced_entry": entry}
    return updated


def _can_access(food_id: int, uid: int) -> bool:
    """Public foods are visible to all; a user food only to its owner."""
    food = get_food_by_id(food_id)
    if not food:
        return False
    if food["source"] in ("user", "recipe", "estimate"):
        return food.get("created_by_user_id") == uid
    return True
