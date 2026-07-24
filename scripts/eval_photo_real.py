"""Photo A/B on the user's OWN captured photos — the ground truth that matters.

Nutrition5k images are overhead cafeteria-rig shots; this replays the REAL phone
photos users captured (persisted by the capture feature), scored against the
user's own human-verified final log (the foods they kept after Undo/Adjust, with
their confirmed calories). So unlike the Nutrition5k photo eval, calorie error is
meaningful here — these are real phone photos with real portions.

Ground truth is read READ-ONLY from the live DB; the pipeline runs against a
SCRATCH DB (never the live one). Each capture's photo is re-run through the photo
agent on the chosen model.

  # OPENROUTER_API_KEY must be in .env for the gemini arm
  uv run python scripts/eval_photo_real.py --tag haiku  --model anthropic:claude-haiku-4-5-20251001
  uv run python scripts/eval_photo_real.py --tag gemini --model openrouter:google/gemini-3.1-flash-lite
  uv run python scripts/eval_photo_real.py --compare

PRIVACY: these are real user food photos; the gemini arm sends them to OpenRouter/
Google (the app already sends photos to Anthropic in prod). Scoped to the maintainer.
"""
import argparse
import asyncio
import glob
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, ".")

LIVE_DB = os.path.join("data", "dictato.db")
EVAL_DIR = os.path.join("data", "evals")
DB_PATH = os.path.join(EVAL_DIR, "real_photo_scratch.db")
os.makedirs(EVAL_DIR, exist_ok=True)
os.environ["DATABASE_PATH"] = DB_PATH        # scratch, set before app import
os.environ["WHISPER_WARMUP"] = "false"

RUN_TAG = "baseline"
OUT_PATH = os.path.join(EVAL_DIR, "realphoto__baseline.jsonl")
_STOP = {"cooked", "raw", "fresh", "grilled", "roasted", "fried", "mixed", "baked",
         "unspecified", "fat", "content", "added", "on", "white", "brown", "large",
         "small", "with", "and", "of", "the", "a", "1", "bun", "patty", "cup", "slices"}


def _tok(name: str) -> set:
    return {w for w in re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
            if w and w not in _STOP and len(w) > 2}


def _matches(gt: str, entry: str) -> bool:
    return bool(_tok(gt) & _tok(entry))


def load_gt() -> list[dict]:
    """READ-ONLY from the live DB: photo captures with a photo on disk and >=1
    surviving (kept) labeled log entry. GT = those foods + their total calories."""
    import sqlite3
    db = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    out = []
    for r in db.execute("SELECT id, photo_path, entries_json FROM capture_log "
                        "WHERE photo_path IS NOT NULL ORDER BY id"):
        if not (r["photo_path"] and os.path.exists(r["photo_path"])):
            continue
        ids = [e.get("id") for e in json.loads(r["entries_json"] or "[]") if e.get("id")]
        foods, cal = [], 0.0
        for eid in ids:
            row = db.execute("SELECT f.name AS name, le.nutrients_snapshot_json AS snap "
                             "FROM log_entries le JOIN foods f ON f.id=le.food_id WHERE le.id=?",
                             (eid,)).fetchone()
            if row:
                foods.append(row["name"])
                cal += (json.loads(row["snap"]).get("calories") or 0)
        if foods:
            out.append({"capture_id": r["id"], "photo_path": r["photo_path"],
                        "gt_foods": foods, "gt_cal": round(cal, 1)})
    db.close()
    return out


def ensure_user() -> int:
    from app.database import init_db, get_conn
    from app.auth import hash_password
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", ("eval@realphoto.local",)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
                           ("eval@realphoto.local", hash_password("evalpass123"), "Real Photo Eval"))
        return cur.lastrowid


def _mime(path: str) -> str:
    return {".png": "image/png", ".webp": "image/webp"}.get(os.path.splitext(path)[1].lower(), "image/jpeg")


async def run_capture(uid: int, cap: dict) -> dict:
    from app.services import agent, llm
    img = open(cap["photo_path"], "rb").read()
    # accepted_* is what the user KEPT (mostly accepted, not verified) — reference
    # only, NOT the score. The score is a human/visual judgement of the photo.
    row = {"capture_id": cap["capture_id"], "photo_path": cap["photo_path"],
           "accepted_foods": cap["gt_foods"], "accepted_cal": cap["gt_cal"],
           "tag": RUN_TAG, "model": llm._resolve_feature("photo")[1]}
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            agent.run_agent(uid, text=None, image=img, image_media_type=_mime(cap["photo_path"]),
                            method="photo"), timeout=300)
    except Exception as e:
        row["pipeline_error"] = repr(e)[:200]
        return row
    row["latency_s"] = round(time.time() - t0, 1)
    row["summary"] = (result.get("summary") or "")[:300]
    row["errored"] = bool(result.get("error"))
    entries = result.get("entries") or []
    row["entry_names"] = [e.get("food_name") for e in entries]
    row["entry_qty_g"] = [round(e.get("quantity_g") or 0, 1) for e in entries]
    row["pipeline_cal"] = round(sum(e.get("calories") or 0 for e in entries), 1)
    src = {}
    for e in entries:
        s = e.get("food_source_raw") or "?"
        src[s] = src.get(s, 0) + 1
    row["sources"] = src
    return row


def _ok(r: dict) -> bool:
    if r.get("errored") or r.get("pipeline_error") or "hit an error" in (r.get("summary") or ""):
        return False
    return r.get("pipeline_cal") is not None


def _load_file(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    by_id = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                by_id[r["capture_id"]] = r
    return [r for r in by_id.values() if _ok(r)]


def ape(p, g):
    return round(abs(p - g) / g * 100, 1) if (g and p is not None) else None


def compare() -> None:
    """No auto-scoring — accepted labels aren't ground truth. Lay each model's
    output side by side per photo so a human/visual judge can score them."""
    files = sorted(glob.glob(os.path.join(EVAL_DIR, "realphoto__*.jsonl")))
    variants = {os.path.basename(p)[len("realphoto__"):-len(".jsonl")]: _load_file(p) for p in files}
    variants = {k: v for k, v in variants.items() if v}
    if not variants:
        print("no results"); return
    print(f"{'variant':10s} {'model':42s} {'n':>3s} {'medCal':>7s} {'medLat':>7s}  sources")
    for tag, rows in variants.items():
        model = (rows[0].get("model") or "?")[:42]
        cal = statistics.median(r["pipeline_cal"] for r in rows if r.get("pipeline_cal") is not None)
        lat = statistics.median(r.get("latency_s") or 0 for r in rows)
        src = {}
        for r in rows:
            for s, n in (r.get("sources") or {}).items():
                src[s] = src.get(s, 0) + n
        print(f"{tag:10s} {model:42s} {len(rows):>3d} {cal:>7.0f} {lat:>6.1f}s  {src}")

    ids = sorted({r["capture_id"] for rows in variants.values() for r in rows})
    print(f"\n--- per-capture side by side ({len(ids)} photos) — judge against the photo ---")
    for cid in ids:
        ref = next((r for rows in variants.values() for r in rows if r["capture_id"] == cid), {})
        print(f"\n  #{cid}  {ref.get('photo_path')}")
        print(f"     accepted(ref only): {ref.get('accepted_foods')}  ~{ref.get('accepted_cal')}kcal")
        for tag, rows in variants.items():
            r = next((x for x in rows if x["capture_id"] == cid), None)
            if not r:
                print(f"     {tag:8s}: (no result)"); continue
            pairs = list(zip(r.get("entry_names") or [], r.get("entry_qty_g") or []))
            names = ", ".join(f"{n} {q:g}g" for n, q in pairs) or "(nothing logged)"
            print(f"     {tag:8s}: {names}  = {r.get('pipeline_cal')}kcal  {r.get('sources')}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()

    global OUT_PATH, RUN_TAG
    if args.tag:
        RUN_TAG, OUT_PATH = args.tag, os.path.join(EVAL_DIR, f"realphoto__{args.tag}.jsonl")
    if args.model:
        os.environ["PHOTO_MODEL"] = args.model
    if args.compare:
        compare(); return

    from app.config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY missing")

    caps = load_gt()
    done = {r["capture_id"] for r in _load_file(OUT_PATH)}
    todo = [c for c in caps if c["capture_id"] not in done]
    print(f"real captures: {len(caps)}  done: {len(done)}  running: {len(todo)}  "
          f"(tag={RUN_TAG}, model={os.environ.get('PHOTO_MODEL', '(default Haiku)')})")

    uid = ensure_user()
    consec = 0
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, cap in enumerate(todo, 1):
            row = await run_capture(uid, cap)
            out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
            print(f"[{i}/{len(todo)}] #{cap['capture_id']} "
                  f"logged {row.get('entry_names', '--')} "
                  f"| {row.get('pipeline_cal', '--')}kcal "
                  f"| src {row.get('sources')} | {row.get('latency_s', '?')}s")
            consec = consec + 1 if not _ok(row) else 0
            if consec >= 3:
                print("\nABORTING: 3 consecutive failures. Re-run to retry."); break
    compare()


if __name__ == "__main__":
    asyncio.run(main())
