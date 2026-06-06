from fastapi import APIRouter, Query, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.auth import get_current_user_id
from app.services.food_lookup import search_foods, get_food_by_id, _row_to_food
from app.services import ai
from app.services.ai_usage import check_and_increment
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT
from app.database import get_conn

router = APIRouter(prefix="/api/foods", tags=["foods"])


class WebLookupRequest(BaseModel):
    name: str
    brand: Optional[str] = None


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


def _can_access(food_id: int, uid: int) -> bool:
    """Public foods are visible to all; a user food only to its owner."""
    food = get_food_by_id(food_id)
    if not food:
        return False
    if food["source"] in ("user", "recipe"):
        return food.get("created_by_user_id") == uid
    return True
