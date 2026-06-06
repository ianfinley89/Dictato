from fastapi import APIRouter, Request, HTTPException
from app.auth import get_current_user_id
from app.models import UserFoodCreate
from app.services import recipes

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


@router.post("/")
async def create(req: UserFoodCreate, request: Request):
    uid = get_current_user_id(request)
    try:
        return recipes.create_user_food(uid, req)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{food_id}")
async def detail(food_id: int, request: Request):
    uid = get_current_user_id(request)
    food = recipes.get_recipe_detail(food_id, uid)
    if not food:
        raise HTTPException(404, "Recipe not found")
    return food


@router.delete("/{food_id}")
async def delete(food_id: int, request: Request):
    uid = get_current_user_id(request)
    result = recipes.delete_user_food(food_id, uid)
    if result == "not_found":
        raise HTTPException(404, "Recipe not found")
    if result == "forbidden":
        raise HTTPException(403, "Forbidden")
    if result == "in_use":
        raise HTTPException(409, "This food has been logged, so it can't be deleted.")
    return {"ok": True}
