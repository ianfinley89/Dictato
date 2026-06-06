"""User-defined foods: recipes (computed from DB ingredients) and custom foods
(manual per-serving macros). Both are stored as `foods` rows scoped to the user,
so the rest of the app (search, voice, serving sizes, logging) treats them like
any other food. Nutrition for recipes is computed from ingredients, never guessed."""
import json
from typing import Optional
from app.database import get_conn
from app.services.food_lookup import get_food_by_id

_MACROS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g")


def _compute_from_ingredients(ingredients: list) -> tuple[dict, float]:
    """Sum ingredient nutrition; returns (totals, total_weight_g)."""
    totals = {k: 0.0 for k in _MACROS}
    weight = 0.0
    for ing in ingredients:
        food = get_food_by_id(ing.food_id)
        if not food:
            raise ValueError(f"Ingredient food {ing.food_id} not found")
        n = food["nutrients_per_100g"]
        factor = ing.quantity_g / 100.0
        for k in _MACROS:
            totals[k] += (n.get(k) or 0) * factor
        weight += ing.quantity_g
    return totals, weight


def create_user_food(user_id: int, req) -> dict:
    """Create a recipe or custom food from a UserFoodCreate request."""
    name = req.name.strip()
    if not name:
        raise ValueError("Name is required")

    if req.ingredients:
        totals, weight = _compute_from_ingredients(req.ingredients)
        if weight <= 0:
            raise ValueError("Ingredients must have positive weight")
        servings = req.servings if req.servings and req.servings > 0 else 1
        serving_g = round(weight / servings, 1)
        per100 = {k: round(totals[k] / weight * 100, 2) for k in _MACROS}
        source = "recipe"
        serving_desc = f"1 {req.serving_label}" if req.serving_label else f"1/{servings:g} of recipe"
    else:
        # Manual per-serving macros → store as "per 100 g" with a nominal 100 g serving,
        # so "1 serving" logs exactly the entered macros.
        if req.calories is None:
            raise ValueError("Provide ingredients or manual calories")
        per100 = {k: float(getattr(req, k) or 0) for k in _MACROS}
        serving_g = 100.0
        source = "user"
        serving_desc = f"1 {req.serving_label}" if req.serving_label else "1 serving"

    per100["micros"] = {}

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO foods (source, name, serving_desc, serving_g, nutrients_json, created_by_user_id)
               VALUES (?,?,?,?,?,?)""",
            (source, name, serving_desc, serving_g, json.dumps(per100), user_id),
        )
        fid = cur.lastrowid
        for ing in req.ingredients:
            conn.execute(
                "INSERT INTO recipe_ingredients (recipe_food_id, ingredient_food_id, quantity_g) VALUES (?,?,?)",
                (fid, ing.food_id, ing.quantity_g),
            )
    return get_food_by_id(fid)


def get_recipe_detail(food_id: int, user_id: int) -> Optional[dict]:
    """A user food plus its ingredient breakdown (for viewing/editing)."""
    food = get_food_by_id(food_id)
    if not food or food["source"] not in ("user", "recipe"):
        return None
    if food.get("created_by_user_id") != user_id:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ri.ingredient_food_id, ri.quantity_g, f.name
               FROM recipe_ingredients ri JOIN foods f ON f.id = ri.ingredient_food_id
               WHERE ri.recipe_food_id=?""",
            (food_id,),
        ).fetchall()
    food["ingredients"] = [
        {"food_id": r["ingredient_food_id"], "name": r["name"], "quantity_g": r["quantity_g"]}
        for r in rows
    ]
    return food


def delete_user_food(food_id: int, user_id: int) -> str:
    """Returns 'ok', 'not_found', 'forbidden', or 'in_use'."""
    food = get_food_by_id(food_id)
    if not food or food["source"] not in ("user", "recipe"):
        return "not_found"
    if food.get("created_by_user_id") != user_id:
        return "forbidden"
    with get_conn() as conn:
        used = conn.execute(
            "SELECT 1 FROM log_entries WHERE food_id=? LIMIT 1", (food_id,)
        ).fetchone()
        if used:
            return "in_use"   # keep it so past log entries still resolve
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_food_id=?", (food_id,))
        conn.execute("DELETE FROM favorites WHERE food_id=?", (food_id,))
        conn.execute("DELETE FROM foods WHERE id=?", (food_id,))
    return "ok"
