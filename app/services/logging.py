"""Writing log entries — shared by the manual log router and the agent tools.

Computes the nutrient snapshot at log time (per hard rules, history stays
stable even if the food row later changes) and enforces the privacy rule that
user-created foods/recipes can only be logged by their owner.
"""
import json
from datetime import datetime, timezone

from app.database import get_conn
from app.services.food_lookup import get_food_by_id


class FoodNotFound(Exception):
    pass


SOURCE_LABELS = {
    "usda": "USDA",
    "off": "Open Food Facts",
    "fatsecret": "FatSecret",
    "user": "Custom (yours)",
    "manual": "Manual",
    "recipe": "Recipe",
    "estimate": "AI estimate",
    "web": "Web (published)",
}


def source_label(conn, food_id: int, food_source: str) -> str:
    """Friendly 'where the nutrition came from'. Recipes show the combo of the
    databases their ingredients came from, e.g. 'Recipe (USDA + FatSecret)'."""
    if food_source == "recipe":
        rows = conn.execute(
            """SELECT DISTINCT f.source FROM recipe_ingredients ri
               JOIN foods f ON f.id = ri.ingredient_food_id
               WHERE ri.recipe_food_id=?""",
            (food_id,),
        ).fetchall()
        parts = sorted({SOURCE_LABELS.get(r["source"], r["source"] or "?") for r in rows})
        return f"Recipe ({' + '.join(parts)})" if parts else "Recipe"
    return SOURCE_LABELS.get(food_source, (food_source or "Unknown").title())


def log_entry_for_user(
    user_id: int,
    food_id: int,
    quantity_g: float,
    source: str,
    notes: str | None = None,
    eaten_at: str | None = None,
) -> dict:
    """Insert a log entry and return it in the shape the frontend renders
    (same fields as _format_entry in the log router, minus eaten_at math)."""
    food = get_food_by_id(food_id)
    if not food:
        raise FoodNotFound(f"Food {food_id} not found")
    if food["source"] in ("user", "recipe", "estimate") and food.get("created_by_user_id") != user_id:
        raise FoodNotFound(f"Food {food_id} not found")

    n = food["nutrients_per_100g"]
    factor = quantity_g / 100.0
    snapshot = {
        "calories": round((n.get("calories") or 0) * factor, 1),
        "protein_g": round((n.get("protein_g") or 0) * factor, 1),
        "carbs_g": round((n.get("carbs_g") or 0) * factor, 1),
        "fat_g": round((n.get("fat_g") or 0) * factor, 1),
        "fiber_g": round((n.get("fiber_g") or 0) * factor, 1),
    }
    eaten_at = eaten_at or datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO log_entries
               (user_id, food_id, eaten_at, quantity_g, nutrients_snapshot_json, source, notes, confirmed)
               VALUES (?,?,?,?,?,?,?,1)""",
            (user_id, food_id, eaten_at, quantity_g, json.dumps(snapshot), source, notes),
        )
        entry_id = cur.lastrowid
        label = source_label(conn, food_id, food["source"])

    return {
        "id": entry_id,
        "food_id": food_id,
        "food_name": food["name"],
        "food_brand": food.get("brand"),
        "eaten_at": eaten_at,
        "quantity_g": quantity_g,
        "source": source,
        "notes": notes,
        "food_source": label,
        "food_source_raw": food["source"],
        "serving_g": food.get("serving_g"),
        "serving_desc": food.get("serving_desc"),
        **snapshot,
    }


def _snapshot(nutrients: dict, quantity_g: float) -> dict:
    factor = quantity_g / 100.0
    return {
        "calories": round((nutrients.get("calories") or 0) * factor, 1),
        "protein_g": round((nutrients.get("protein_g") or 0) * factor, 1),
        "carbs_g": round((nutrients.get("carbs_g") or 0) * factor, 1),
        "fat_g": round((nutrients.get("fat_g") or 0) * factor, 1),
        "fiber_g": round((nutrients.get("fiber_g") or 0) * factor, 1),
    }


def update_entry_quantity(user_id: int, entry_id: int, quantity_g: float) -> dict:
    """Change an entry's quantity and recompute its snapshot. Used by the
    follow-up refinement flow ('say more' / 'add photo' after logging)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, food_id FROM log_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not row or row["user_id"] != user_id:
            raise FoodNotFound(f"Entry {entry_id} not found")
    food = get_food_by_id(row["food_id"])
    if not food:
        raise FoodNotFound(f"Food for entry {entry_id} not found")
    snapshot = _snapshot(food["nutrients_per_100g"], quantity_g)
    with get_conn() as conn:
        conn.execute(
            "UPDATE log_entries SET quantity_g=?, nutrients_snapshot_json=? WHERE id=?",
            (quantity_g, json.dumps(snapshot), entry_id),
        )
        label = source_label(conn, food["id"], food["source"])
    return {"id": entry_id, "food_id": food["id"], "food_name": food["name"],
            "food_brand": food.get("brand"), "quantity_g": quantity_g,
            "food_source": label, "food_source_raw": food["source"],
            "serving_g": food.get("serving_g"), "serving_desc": food.get("serving_desc"),
            **snapshot}


def remove_entry(user_id: int, entry_id: int) -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM log_entries WHERE id=?", (entry_id,)).fetchone()
        if not row or row["user_id"] != user_id:
            raise FoodNotFound(f"Entry {entry_id} not found")
        conn.execute("DELETE FROM log_entries WHERE id=?", (entry_id,))


def current_entries(user_id: int, entry_ids: list[int]) -> list[dict]:
    """The live state of a set of entries (post any undo/adjust/revision) in the
    same shape the result card renders."""
    if not entry_ids:
        return []
    marks = ",".join("?" * len(entry_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT le.*, f.name AS food_name, f.brand AS food_brand, f.source AS food_source_raw,
                       f.serving_g, f.serving_desc
                FROM log_entries le JOIN foods f ON f.id = le.food_id
                WHERE le.user_id=? AND le.id IN ({marks}) ORDER BY le.id""",
            (user_id, *entry_ids),
        ).fetchall()
        out = []
        for r in rows:
            snap = json.loads(r["nutrients_snapshot_json"])
            out.append({
                "id": r["id"], "food_id": r["food_id"], "food_name": r["food_name"],
                "food_brand": r["food_brand"], "eaten_at": r["eaten_at"],
                "quantity_g": r["quantity_g"], "source": r["source"], "notes": r["notes"],
                "food_source": source_label(conn, r["food_id"], r["food_source_raw"]),
                "food_source_raw": r["food_source_raw"],
                "serving_g": r["serving_g"], "serving_desc": r["serving_desc"], **snap,
            })
    return out
