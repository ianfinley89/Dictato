"""Phase-2 photo eval: run Nutrition5k overhead images through the photo
pipeline and A/B the vision model (Haiku vs Gemini Flash Lite).

Unlike the text evals, this is PHOTO mode — the image goes through the real
agent (vision model -> search_food_db -> grounding). Scoring focuses on what a
camera can actually judge:
  - INGREDIENT RECALL on VISIBLE major GT ingredients (mass >= 15g; the GT also
    lists invisible sauces/seasonings/sugar/salt that no model can see, so those
    are excluded). Fuzzy noun-match after stripping prep/color adjectives.
  - EXTRA entries (logged foods that match no GT ingredient — over-decomposition
    or hallucination).
  - SOURCE distribution (grounding behavior).
  - calorie error vs GT total (SECONDARY + noisy: portion from a fixed overhead
    rig shot is hard — but both models face the identical image, so the A/B is
    a fair RELATIVE comparison).
  - tokens (cost proxy) + latency, per model.

    # set OPENROUTER_API_KEY in .env first (Gemini side)
    uv run python scripts/eval_photo.py --n 15 --tag haiku  --model anthropic:claude-haiku-4-5-20251001
    uv run python scripts/eval_photo.py --n 15 --tag gemini --model openrouter:google/gemini-3.1-flash-lite-image
    uv run python scripts/eval_photo.py --compare

Real vision-call spend; scratch DB (data/evals/photo_scratch.db); never live.
"""
import argparse
import asyncio
import csv
import glob
import json
import os
import re
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, ".")

EVAL_DIR = os.path.join("data", "evals")
IMG_DIR = os.path.join(EVAL_DIR, "n5k_images")
DB_PATH = os.path.join(EVAL_DIR, "photo_scratch.db")
IMG_BASE = ("https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/"
            "imagery/realsense_overhead")
META = os.path.join(EVAL_DIR, "dish_metadata_cafe1.csv")   # cafe1 has the overhead RGB rig

os.makedirs(IMG_DIR, exist_ok=True)
os.environ["DATABASE_PATH"] = DB_PATH
os.environ["WHISPER_WARMUP"] = "false"

RUN_TAG = "baseline"
OUT_PATH = os.path.join(EVAL_DIR, "photo__baseline.jsonl")
MACROS = ("calories", "protein_g", "carbs_g", "fat_g")
# Adjectives to strip so the core food noun matches ("grilled chicken" ~ "chicken breast").
_STOP = {"grilled", "roasted", "fried", "raw", "fresh", "cooked", "mixed", "baked",
         "steamed", "sauteed", "boiled", "scrambled", "white", "brown", "red", "green",
         "yellow", "small", "large", "sliced", "chopped", "diced", "whole", "of", "a",
         "the", "with", "and", "in", "on", "plain", "side", "cup", "slices", "slice"}


def _tokens(name: str) -> set:
    return {w for w in re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
            if w and w not in _STOP and len(w) > 2}


def _matches(gt_name: str, entry_name: str) -> bool:
    a, b = _tokens(gt_name), _tokens(entry_name)
    return bool(a & b)


def parse_dish(row: list[str]) -> dict | None:
    if len(row) < 13 or (len(row) - 6) % 7 != 0:
        return None
    try:
        ingrs = []
        for i in range(6, len(row), 7):
            name, grams = row[i + 1].strip().lower(), float(row[i + 2])
            if name == "deprecated" or grams < 2.0:
                continue
            ingrs.append({"name": name, "grams": round(grams, 1),
                          **{m: float(row[i + 3 + j]) for j, m in
                             enumerate(("calories", "fat_g", "carbs_g", "protein_g"))}})
    except (ValueError, IndexError):
        return None
    if not (2 <= len(ingrs) <= 8):
        return None
    gt = {m: round(sum(x[m] for x in ingrs), 1) for m in MACROS}
    if not (80 <= gt["calories"] <= 2000):
        return None
    return {"dish_id": row[0], "ingredients": ingrs, "gt": gt,
            "visible": [x["name"] for x in ingrs if x["grams"] >= 15]}


def load_dishes() -> list[dict]:
    if not os.path.exists(META):
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/"
            "metadata/dish_metadata_cafe1.csv", META)
    out = []
    with open(META, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            d = parse_dish(row)
            if d and d["visible"]:
                out.append(d)
    return out


def fetch_image(dish_id: str) -> str | None:
    path = os.path.join(IMG_DIR, f"{dish_id}.png")
    if os.path.exists(path):
        return path
    try:
        req = urllib.request.Request(f"{IMG_BASE}/{dish_id}/rgb.png")
        data = urllib.request.urlopen(req, timeout=40).read()
        if len(data) < 5000:
            return None
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


def ensure_user() -> int:
    from app.database import init_db, get_conn
    from app.auth import hash_password
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", ("eval@photo.local",)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
                           ("eval@photo.local", hash_password("evalpass123"), "Photo Eval"))
        return cur.lastrowid


async def run_dish(uid: int, dish: dict, img_path: str) -> dict:
    from app.services import agent, llm
    img = open(img_path, "rb").read()
    row = {"dish_id": dish["dish_id"], "gt": dish["gt"], "visible": dish["visible"],
           "tag": RUN_TAG, "model": llm._resolve_feature("photo")[1]}
    t0 = time.time()
    try:
        import base64
        result = await asyncio.wait_for(
            agent.run_agent(uid, text=None, image=img, image_media_type="image/png",
                            method="photo"), timeout=300)
    except Exception as e:
        row["pipeline_error"] = repr(e)[:200]
        return row
    row["latency_s"] = round(time.time() - t0, 1)
    row["summary"] = (result.get("summary") or "")[:300]
    row["errored"] = bool(result.get("error"))
    entries = result.get("entries") or []
    row["n_entries"] = len(entries)
    row["pipeline"] = {m: round(sum(e.get(m) or 0 for e in entries), 1) for m in MACROS}
    row["entry_names"] = [e.get("food_name") for e in entries]
    src = {}
    for e in entries:
        s = e.get("food_source_raw") or "?"
        src[s] = src.get(s, 0) + 1
    row["sources"] = src
    # Recognition: visible GT ingredients matched by any logged entry.
    matched = [g for g in dish["visible"] if any(_matches(g, n) for n in row["entry_names"])]
    row["recall"] = round(len(matched) / len(dish["visible"]), 3)
    row["matched"], row["missed"] = matched, [g for g in dish["visible"] if g not in matched]
    row["extra"] = [n for n in row["entry_names"] if not any(_matches(g, n) for g in dish["visible"])]
    return row


def _ok(r: dict) -> bool:
    if r.get("errored") or r.get("pipeline_error") or "hit an error" in (r.get("summary") or ""):
        return False
    return bool(r.get("pipeline"))


def _load_file(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    by_id = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                by_id[r["dish_id"]] = r
    return [r for r in by_id.values() if _ok(r)]


def ape(p, g):
    return round(abs(p - g) / g * 100, 1) if (g and p is not None) else None


def report(rows) -> None:
    print(f"scored: {len(rows)}")
    if not rows:
        return
    rec = statistics.median(r["recall"] for r in rows)
    cal = [ape(r["pipeline"]["calories"], r["gt"]["calories"]) for r in rows]
    cal = [x for x in cal if x is not None]
    extra = statistics.median(len(r["extra"]) for r in rows)
    src = {}
    for r in rows:
        for s, n in (r.get("sources") or {}).items():
            src[s] = src.get(s, 0) + n
    print(f"  ingredient recall (visible): median {rec:.2f}")
    print(f"  calorie %err (noisy): median {statistics.median(cal):.1f}%")
    print(f"  extra entries/dish: median {extra:.0f}   sources: {src}")


def compare() -> None:
    files = sorted(glob.glob(os.path.join(EVAL_DIR, "photo__*.jsonl")))
    variants = {os.path.basename(p)[len("photo__"):-len(".jsonl")]: _load_file(p) for p in files}
    variants = {k: v for k, v in variants.items() if v}
    if not variants:
        print("no results"); return
    print(f"{'variant':10s} {'model':40s} {'n':>3s} {'recall':>7s} {'cal%':>6s} {'extra':>6s}  sources")
    for tag, rows in variants.items():
        model = (rows[0].get("model") or "?")[:40]
        rec = statistics.median(r["recall"] for r in rows)
        cal = [ape(r["pipeline"]["calories"], r["gt"]["calories"]) for r in rows]
        cal = [x for x in cal if x is not None]
        extra = statistics.median(len(r["extra"]) for r in rows)
        src = {}
        for r in rows:
            for s, n in (r.get("sources") or {}).items():
                src[s] = src.get(s, 0) + n
        print(f"{tag:10s} {model:40s} {len(rows):>3d} {rec:>7.2f} "
              f"{statistics.median(cal):>5.1f}% {extra:>6.0f}  {src}")
    # Head-to-head recall on shared images
    tags = list(variants)
    if len(tags) == 2:
        a, b = tags
        amap = {r["dish_id"]: r for r in variants[a]}
        both = [(amap[r["dish_id"]], r) for r in variants[b] if r["dish_id"] in amap]
        awins = sum(1 for x, y in both if x["recall"] > y["recall"] + 0.01)
        bwins = sum(1 for x, y in both if y["recall"] > x["recall"] + 0.01)
        print(f"\nshared images: {len(both)}  {a} better recall: {awins}  {b} better recall: {bwins}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--tag", default="")
    ap.add_argument("--model", default="", help="provider:model for the photo feature this run")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    global OUT_PATH, RUN_TAG
    if args.tag:
        RUN_TAG, OUT_PATH = args.tag, os.path.join(EVAL_DIR, f"photo__{args.tag}.jsonl")
    if args.model:
        os.environ["PHOTO_MODEL"] = args.model
    if args.compare:
        compare(); return
    if args.report:
        report(_load_file(OUT_PATH)); return

    from app.config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY missing")

    import random
    dishes = load_dishes()
    random.Random(7).shuffle(dishes)
    done = {r["dish_id"] for r in _load_file(OUT_PATH)}
    todo, consec_err = [], 0
    for d in dishes:
        if len(todo) >= max(0, args.n - len(done)):
            break
        if d["dish_id"] in done:
            continue
        if fetch_image(d["dish_id"]):     # only dishes whose overhead image exists
            todo.append(d)
    print(f"done: {len(done)}  running: {len(todo)}  (tag={RUN_TAG}, "
          f"photo model set to {os.environ.get('PHOTO_MODEL', '(default Haiku)')})")

    uid = ensure_user()
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, dish in enumerate(todo, 1):
            row = await run_dish(uid, dish, os.path.join(IMG_DIR, f"{dish['dish_id']}.png"))
            out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
            print(f"[{i}/{len(todo)}] {dish['dish_id']} recall {row.get('recall', '--')} "
                  f"| cal {row.get('pipeline', {}).get('calories', '--')}/{dish['gt']['calories']:.0f} "
                  f"| src {row.get('sources')} | {row.get('latency_s', '?')}s")
            consec_err = consec_err + 1 if not _ok(row) else 0
            if consec_err >= 3:
                print("\nABORTING: 3 consecutive failures (API/credit?). Re-run to retry.")
                break
    report(_load_file(OUT_PATH))


if __name__ == "__main__":
    asyncio.run(main())
