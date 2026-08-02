"""Export supervised training examples from the capture log as JSONL.

Each line is one capture chain (an original capture plus its "say more" /
"add photo" follow-ups), labeled with the FINAL human-verified state:
entries the user kept (with their corrected quantities from the live log)
and entries they undid (kept=false — a negative signal worth training on).
Issue reports filed against a capture ride along under "issues" — a 'model'
report marks the example as a known miss, a 'capture' report flags its audio
for the STT eval set.

    uv run python scripts/export_dataset.py [out.jsonl]

Photos and voice recordings are referenced by path (files live under
UPLOAD_DIR/captures/).
"""
import json
import sys

sys.path.insert(0, ".")

from app.database import get_conn


def _entry_label(conn, snap: dict) -> dict:
    """A snapshot entry, corrected to its live state.

    The snapshot is what the MODEL produced; the label has to be what the user
    ended up with. Three corrections can arrive after the fact: the entry is
    undone (kept=False), its quantity is fixed, or the food itself is renamed —
    "kodak protein pancakes" becoming "kodiak protein pancakes" is exactly the
    signal worth training on, and reading the frozen name would throw it away.
    """
    label = {k: snap.get(k) for k in
             ("food_name", "food_brand", "quantity_g", "food_source",
              "calories", "protein_g", "carbs_g", "fat_g", "fiber_g")}
    label["model_food_name"] = snap.get("food_name")   # what it originally said
    live = None
    if snap.get("id"):
        live = conn.execute(
            """SELECT le.quantity_g, le.nutrients_snapshot_json, le.portion_manual,
                      f.name AS food_name, f.brand AS food_brand
               FROM log_entries le JOIN foods f ON f.id = le.food_id
               WHERE le.id = ?""",
            (snap["id"],),
        ).fetchone()
    label["kept"] = live is not None
    if live:
        label["quantity_g"] = live["quantity_g"]
        label["food_name"] = live["food_name"]
        label["food_brand"] = live["food_brand"]
        # The user set this amount by hand — a verified portion, not an accepted one.
        label["portion_corrected"] = bool(live["portion_manual"])
        label.update(json.loads(live["nutrients_snapshot_json"]))
    if label.get("model_food_name") == label.get("food_name"):
        label.pop("model_food_name")           # only note it when it changed
    return label


def export(out=sys.stdout) -> int:
    n = 0
    with get_conn() as conn:
        roots = conn.execute(
            "SELECT * FROM capture_log WHERE parent_capture_id IS NULL ORDER BY id"
        ).fetchall()
        for root in roots:
            chain = [root] + conn.execute(
                "SELECT * FROM capture_log WHERE parent_capture_id=? ORDER BY id",
                (root["id"],),
            ).fetchall()
            last = chain[-1]   # the most-refined snapshot of what was logged

            example = {
                "capture_id": root["id"],
                "user_id": root["user_id"],
                "created_at": root["created_at"],
                "inputs": [
                    {"type": c["input_type"], "text": c["transcript"],
                     "image": c["photo_path"], "audio": c["audio_path"]}
                    for c in chain
                ],
                "meal": last["meal"] or root["meal"],
                "meal_label": last["meal_label"] or root["meal_label"],
                "tags": json.loads((last["tags_json"] or root["tags_json"]) or "[]"),
                "specificity": last["specificity"] or root["specificity"],
                "items": [_entry_label(conn, s)
                          for s in json.loads(last["entries_json"] or "[]")],
            }
            chain_ids = [c["id"] for c in chain]
            issue_rows = conn.execute(
                f"""SELECT category, message FROM issue_reports
                    WHERE capture_id IN ({','.join('?' * len(chain_ids))})""",
                chain_ids,
            ).fetchall()
            if issue_rows:
                example["issues"] = [{"category": r["category"], "message": r["message"]}
                                     for r in issue_rows]
            out.write(json.dumps(example, ensure_ascii=False) + "\n")
            n += 1
    return n


def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as f:
            n = export(f)
        print(f"Wrote {n} examples to {sys.argv[1]}")
    else:
        export()


if __name__ == "__main__":
    main()
