"""Menu-Match grounding eval: the HARD half of rule #1.

Nutrition5k is all generic whole foods (USDA's sweet spot), so it grounds 100%
USDA and never exercises the web/estimate fallback. Menu-Match is the opposite:
41 named RESTAURANT dishes (orange chicken, panang curry, pepperoni pizza) with
dietitian-verified per-serving calories. Composite, prepared, portion-variable —
exactly where the DB->web_search->create_food fallback should actually fire.

Per item we phrase a natural restaurant-order transcript ("I had an order of
Orange chicken at an asian restaurant") and run it through the production
`agent.run_agent`, then measure:
  - SOURCE DISTRIBUTION — does the fallback activate here (web/estimate/non-USDA)
    where it stayed dormant on Nutrition5k? This is the primary signal.
  - rule-#1 SUSPECTS — web/estimate entries that search_foods() shows had a real
    DB candidate (invented-when-groundable).
  - calorie error vs the dietitian ground truth (SECONDARY + noisy: restaurant
    portions vary hugely, and a generic lookup legitimately differs from one
    restaurant's specific serving — read the trend, not any single dish).
  - a no-tools BASELINE (raw model guess) for the correction delta.

    uv run python scripts/eval_menumatch.py --n 41        # run (resumable)
    uv run python scripts/eval_menumatch.py --report
    uv run python scripts/eval_menumatch.py --n 5 --fresh

Separate scratch DB from the Nutrition5k eval so its generic-food cache can't
bias restaurant grounding. Real Anthropic + USDA/OFF calls; never the live DB.
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, ".")

EVAL_DIR = os.path.join("data", "evals")
MM_DIR = os.path.join(EVAL_DIR, "menumatch")
ITEMS_PATH = os.path.join(MM_DIR, "items_info.txt")
DB_PATH = os.path.join(EVAL_DIR, "mm_scratch.db")
OUT_PATH = os.path.join(EVAL_DIR, "menumatch_grounding.jsonl")

os.makedirs(EVAL_DIR, exist_ok=True)
os.environ["DATABASE_PATH"] = DB_PATH        # must precede any app import
os.environ["WHISPER_WARMUP"] = "false"

DB_SOURCES = ("usda", "off", "fatsecret")


def load_items() -> list[dict]:
    """items_info.txt: 'Name on Menu; label; Calories; Restaurant' (semicolons,
    padded with tabs/spaces)."""
    items = []
    with open(ITEMS_PATH, encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 4 or not parts[0]:
                continue
            try:
                cal = float(parts[2])
            except ValueError:
                continue
            items.append({"name": parts[0], "label": parts[1], "cal_gt": cal,
                          "restaurant": parts[3]})
    return items


def transcript_for(item: dict) -> str:
    art = "an" if item["restaurant"][0] in "aeiou" else "a"
    return f"I had an order of {item['name']} at {art} {item['restaurant']} restaurant."


def ensure_user() -> int:
    from app.database import init_db, get_conn
    from app.auth import hash_password
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", ("eval@mm.local",)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            ("eval@mm.local", hash_password("evalpass123"), "MM Eval"))
        return cur.lastrowid


async def baseline_estimate(transcript: str) -> float | None:
    from app.services import llm
    system = ('Estimate the TOTAL calories of the described restaurant order. '
              'Reply with ONLY a number, no units, no prose.')
    try:
        resp = await llm.chat(feature="voice", system=system,
                              messages=[{"role": "user", "text": transcript}], max_tokens=50)
        m = re.search(r"\d+(\.\d+)?", resp.text or "")
        return float(m.group(0)) if m else None
    except Exception:
        return None


async def probe_db_candidates(name: str, uid: int) -> list[str]:
    from app.services.food_lookup import search_foods
    try:
        hits = await search_foods(name, uid, limit=5)
        return [f"{h['name']} [{h['source']}]" for h in hits if h["source"] in DB_SOURCES][:3]
    except Exception:
        return []


async def run_item(uid: int, item: dict) -> dict:
    from app.services import agent
    transcript = transcript_for(item)
    row = {"name": item["name"], "restaurant": item["restaurant"],
           "cal_gt": item["cal_gt"], "transcript": transcript}
    row["baseline_cal"] = await baseline_estimate(transcript)

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            agent.run_agent(uid, text=transcript, image=None,
                            image_media_type=None, method="voice"), timeout=300)
    except Exception as e:
        row["pipeline_error"] = repr(e)[:200]
        return row
    row["latency_s"] = round(time.time() - t0, 1)
    row["summary"] = (result.get("summary") or "")[:300]
    entries = result.get("entries") or []
    row["n_entries"] = len(entries)
    row["pipeline_cal"] = round(sum(e.get("calories") or 0 for e in entries), 1)
    row["entries"] = [{"name": e.get("food_name"), "grams": e.get("quantity_g"),
                       "calories": e.get("calories"), "source": e.get("food_source_raw")}
                      for e in entries]
    src = {}
    for e in entries:
        s = e.get("food_source_raw") or "?"
        src[s] = src.get(s, 0) + 1
    row["sources"] = src
    suspects = []
    for e in entries:
        if e.get("food_source_raw") in ("web", "estimate"):
            cands = await probe_db_candidates(e.get("food_name") or "", uid)
            suspects.append({"food": e.get("food_name"), "tier": e.get("food_source_raw"),
                             "db_candidates": cands})
    if suspects:
        row["fallback_probe"] = suspects
    return row


def load_rows() -> list[dict]:
    if not os.path.exists(OUT_PATH):
        return []
    with open(OUT_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def ape(pred, gt):
    return round(abs(pred - gt) / gt * 100, 1) if (gt and pred is not None) else None


def report() -> None:
    rows = load_rows()
    done = [r for r in rows if r.get("pipeline_cal") is not None]
    print(f"items: {len(rows)}  scored: {len(done)}  errors: {len(rows) - len(done)}")
    if not done:
        return

    b = [ape(r["baseline_cal"], r["cal_gt"]) for r in done]
    p = [ape(r["pipeline_cal"], r["cal_gt"]) for r in done]
    b, p = [x for x in b if x is not None], [x for x in p if x is not None]
    print("\n--- median abs calorie % error vs dietitian GT (noisy: restaurant portions vary) ---")
    print(f"baseline (raw model) {statistics.median(b):6.1f}%    pipeline {statistics.median(p):6.1f}%")

    # THE headline: did the fallback path activate (vs Nutrition5k's 100% USDA)?
    src_totals, entries_total = {}, 0
    for r in done:
        for s, n in (r.get("sources") or {}).items():
            src_totals[s] = src_totals.get(s, 0) + n
            entries_total += n
    print("\n--- entry sources (Nutrition5k was 100% usda — here we expect the fallback to fire) ---")
    for s, n in sorted(src_totals.items(), key=lambda kv: -kv[1]):
        print(f"{s:10s} {n:4d}  ({n / (entries_total or 1):.0%})")
    fell_back = sum(1 for r in done
                    if r.get("sources") and any(s not in ("usda",) for s in r["sources"]))
    print(f"items using any non-USDA source: {fell_back}/{len(done)}")

    suspects = [(r["name"], s) for r in done for s in r.get("fallback_probe") or []
                if s["db_candidates"]]
    print(f"\n--- rule-#1 suspects (web/estimate despite DB candidates): {len(suspects)} ---")
    for name, s in suspects[:12]:
        print(f"{name[:28]:28s} {s['tier']:8s} {s['food']}  cand: {'; '.join(s['db_candidates'])}")

    print("\n--- by restaurant: median pipeline cal %err ---")
    for r_name in ("asian", "italian", "soup"):
        errs = [ape(r["pipeline_cal"], r["cal_gt"]) for r in done if r["restaurant"] == r_name]
        errs = [e for e in errs if e is not None]
        if errs:
            print(f"{r_name:8s} {statistics.median(errs):6.1f}%  (n={len(errs)})")

    worst = sorted(done, key=lambda r: -(ape(r["pipeline_cal"], r["cal_gt"]) or 0))[:6]
    print("\n--- worst 6 by pipeline calorie error ---")
    for r in worst:
        print(f"{r['name'][:30]:30s} gt {r['cal_gt']:.0f} -> {r['pipeline_cal']:.0f} "
              f"({ape(r['pipeline_cal'], r['cal_gt'])}%)  src {r.get('sources')}  "
              f"[{'; '.join(e['name'] for e in r['entries'])[:60]}]")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=41)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    if args.report:
        report(); return
    if args.fresh:
        for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm", OUT_PATH):
            if os.path.exists(p):
                os.remove(p)

    from app.config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY missing")

    items = load_items()
    seen = {r["name"] for r in load_rows()}
    todo = [it for it in items if it["name"] not in seen][:max(0, args.n - len(seen))]
    print(f"menu items: {len(items)}  done: {len(seen)}  running: {len(todo)}")
    if not todo:
        report(); return

    uid = ensure_user()
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, item in enumerate(todo, 1):
            row = await run_item(uid, item)
            out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
            print(f"[{i}/{len(todo)}] {item['name'][:26]:26s} gt {item['cal_gt']:.0f} | "
                  f"pipe {row.get('pipeline_cal', '--')} | base {row.get('baseline_cal', '--')} | "
                  f"src {row.get('sources')} | {row.get('latency_s', '?')}s")
    report()


if __name__ == "__main__":
    asyncio.run(main())
