"""Grounding eval: replay Nutrition5k dishes through the production agent loop.

Phase 1 (text-mode, known-mass): each dish's ground-truth ingredient list WITH
its true scale-measured grams is phrased as a transcript and run through the
real `agent.run_agent` — the actual search_food_db -> USDA/OFF/FatSecret ->
web_search -> create_food tool loop, on a SCRATCH database. Because the true
grams are stated, portion guessing is removed from the equation: remaining
error is pure lookup/grounding error (hard rule #1 territory).

Per dish we also take a no-tools BASELINE: the same model asked to estimate the
meal's totals directly from the same transcript. baseline error minus pipeline
error = how much correction the grounding architecture buys.

Per-entry `food_source` gives step attribution (usda/off/fatsecret = DB,
web = web-grounded, estimate = model-invented). Entries sourced from
web/estimate are probed against search_foods() afterwards: if a real DB
candidate existed, that's a hard-rule-#1 violation suspect, listed for review.

    uv run python scripts/eval_grounding.py --n 50       # run (resumable)
    uv run python scripts/eval_grounding.py --report     # summarize results
    uv run python scripts/eval_grounding.py --n 3 --fresh  # wipe scratch first

Results: data/evals/n5k_grounding.jsonl (data/ is gitignored).
Uses the REAL Anthropic + USDA/OFF APIs against a SCRATCH DB — never the live one.
GT note: dish-total columns in cafe2 are sometimes zeroed, so ground truth is
the SUM of per-ingredient values (USDA-derived, tied to scale-measured mass).
"""
import argparse
import asyncio
import csv
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, ".")

EVAL_DIR = os.path.join("data", "evals")
DB_PATH = os.path.join(EVAL_DIR, "eval_scratch.db")
OUT_PATH = os.path.join(EVAL_DIR, "n5k_grounding.jsonl")
META_URLS = {
    "cafe1": "https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/metadata/dish_metadata_cafe1.csv",
    "cafe2": "https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/metadata/dish_metadata_cafe2.csv",
}

os.makedirs(EVAL_DIR, exist_ok=True)
# Must be set BEFORE any app import so the scratch DB wins over .env.
os.environ["DATABASE_PATH"] = DB_PATH
os.environ["WHISPER_WARMUP"] = "false"

RUN_TAG = "baseline"          # a prompt/model variant label; set by --tag
COOKED_RE = re.compile(r"\b(fried|scrambled|roasted|hash|bacon|sausage|sauteed|"
                       r"breaded|grilled|baked|egg|omelet)\w*", re.I)

DB_SOURCES = ("usda", "off", "fatsecret")
MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


# ── Nutrition5k metadata ──────────────────────────────────────────────────────

def fetch_metadata() -> list[dict]:
    dishes = []
    for cafe, url in META_URLS.items():
        path = os.path.join(EVAL_DIR, f"dish_metadata_{cafe}.csv")
        if not os.path.exists(path):
            print(f"downloading {cafe} metadata...")
            urllib.request.urlretrieve(url, path)
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                d = parse_dish(row, cafe)
                if d:
                    dishes.append(d)
    return dishes


def parse_dish(row: list[str], cafe: str) -> dict | None:
    """dish_id, total_cal, total_mass, total_fat, total_carb, total_protein,
    then repeating (ingr_id, name, grams, cal, fat, carb, protein)."""
    if len(row) < 13 or (len(row) - 6) % 7 != 0:
        return None
    try:
        ingrs = []
        for i in range(6, len(row), 7):
            name = row[i + 1].strip().lower()
            grams = float(row[i + 2])
            if name == "deprecated" or grams < 2.0:
                continue
            ingrs.append({
                "name": name, "grams": round(grams, 1),
                "calories": float(row[i + 3]), "fat_g": float(row[i + 4]),
                "carbs_g": float(row[i + 5]), "protein_g": float(row[i + 6]),
            })
    except (ValueError, IndexError):
        return None
    if not (2 <= len(ingrs) <= 6):
        return None
    gt = {m: round(sum(i[m] for i in ingrs), 1) for m in MACROS}
    if not (50 <= gt["calories"] <= 3000):
        return None
    return {"dish_id": row[0], "cafe": cafe, "ingredients": ingrs, "gt": gt}


def transcript_for(dish: dict) -> str:
    parts = [f"{round(i['grams'])} grams of {i['name']}" for i in dish["ingredients"]]
    if len(parts) > 1:
        parts[-1] = "and " + parts[-1]
    return "I ate a meal with " + ", ".join(parts) + "."


# ── Scratch-DB setup ──────────────────────────────────────────────────────────

def ensure_user() -> int:
    from app.database import init_db, get_conn
    from app.auth import hash_password
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", ("eval@n5k.local",)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            ("eval@n5k.local", hash_password("evalpass123"), "N5K Eval"),
        )
        return cur.lastrowid


# ── Stages ────────────────────────────────────────────────────────────────────

async def baseline_estimate(transcript: str) -> dict | None:
    """No-tools model guess from the same transcript — the 'raw Haiku' stage."""
    from app.services import llm
    system = ('You are a nutrition expert. Estimate the TOTAL nutrition of the described '
              'meal. Reply with ONLY a JSON object, no prose: '
              '{"calories": <number>, "protein_g": <number>, "carbs_g": <number>, "fat_g": <number>}')
    try:
        resp = await llm.chat(feature="voice", system=system,
                              messages=[{"role": "user", "text": transcript}], max_tokens=200)
        m = re.search(r"\{[^{}]*\}", resp.text or "")
        if not m:
            return None
        d = json.loads(m.group(0))
        return {k: float(d.get(k, 0) or 0) for k in MACROS}
    except Exception:
        return None


async def probe_db_candidates(name: str, uid: int) -> list[str]:
    """Did the DB actually have candidates for this food? (rule-#1 probe)"""
    from app.services.food_lookup import search_foods
    try:
        hits = await search_foods(name, uid, limit=5)
        return [f"{h['name']} [{h['source']}]" for h in hits if h["source"] in DB_SOURCES][:3]
    except Exception:
        return []


async def run_dish(uid: int, dish: dict) -> dict:
    from app.services import agent, llm
    transcript = transcript_for(dish)
    row = {"dish_id": dish["dish_id"], "cafe": dish["cafe"], "transcript": transcript,
           "n_ingredients": len(dish["ingredients"]), "gt": dish["gt"],
           "tag": RUN_TAG, "model": llm._resolve_feature("voice")[1]}

    row["baseline"] = await baseline_estimate(transcript)

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            agent.run_agent(uid, text=transcript, image=None,
                            image_media_type=None, method="voice"),
            timeout=300)
    except Exception as e:
        row["pipeline_error"] = repr(e)[:200]
        return row
    row["latency_s"] = round(time.time() - t0, 1)
    row["turns"] = result.get("turns")
    row["agent_error"] = bool(result.get("error"))
    row["summary"] = (result.get("summary") or "")[:300]

    entries = result.get("entries") or []
    row["n_entries"] = len(entries)
    row["pipeline"] = {m: round(sum(e.get(m) or 0 for e in entries), 1) for m in MACROS}
    row["entries"] = [{"name": e.get("food_name"), "grams": e.get("quantity_g"),
                       "calories": e.get("calories"), "source": e.get("food_source_raw")}
                      for e in entries]
    sources = {}
    for e in entries:
        s = e.get("food_source_raw") or "?"
        sources[s] = sources.get(s, 0) + 1
    row["sources"] = sources

    # Rule-#1 probe: web/estimate entries that a DB search could have grounded.
    suspects = []
    for e in entries:
        if e.get("food_source_raw") in ("web", "estimate"):
            cands = await probe_db_candidates(e.get("food_name") or "", uid)
            suspects.append({"food": e.get("food_name"), "tier": e.get("food_source_raw"),
                             "db_candidates": cands})
    if suspects:
        row["fallback_probe"] = suspects
    return row


# ── Reporting ─────────────────────────────────────────────────────────────────

def ape(pred: float, gt: float) -> float | None:
    return round(abs(pred - gt) / gt * 100, 1) if gt else None


def _ok(r: dict) -> bool:
    """A usable result: entries produced, no agent/API error. A credit/API
    failure is not a data point — it must be retried on resume. The summary
    check catches legacy rows written before the error flags existed."""
    if r.get("agent_error") or r.get("pipeline_error") or "hit an error" in (r.get("summary") or ""):
        return False
    return bool(r.get("pipeline"))


def load_rows() -> list[dict]:
    """Deduped by dish_id, last write wins (a retried dish supersedes its failure)."""
    if not os.path.exists(OUT_PATH):
        return []
    by_id = {}
    with open(OUT_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                by_id[r["dish_id"]] = r
    return list(by_id.values())


def report() -> None:
    rows = load_rows()
    done = [r for r in rows if _ok(r)]
    print(f"rows: {len(rows)}  scored: {len(done)}  "
          f"errored/excluded: {len(rows) - len(done)} (API/credit failures — re-run to retry)")
    if not done:
        return

    print("\n--- median abs % error (baseline = raw model, pipeline = full agent) ---")
    for m in MACROS:
        b = [ape(r["baseline"][m], r["gt"][m]) for r in done
             if r.get("baseline") and r["gt"][m]]
        p = [ape(r["pipeline"][m], r["gt"][m]) for r in done if r["gt"][m]]
        b, p = [x for x in b if x is not None], [x for x in p if x is not None]
        print(f"{m:10s}  baseline {statistics.median(b):6.1f}%   pipeline {statistics.median(p):6.1f}%"
              if b and p else f"{m:10s}  (insufficient)")

    src_totals: dict = {}
    for r in done:
        for s, n in (r.get("sources") or {}).items():
            src_totals[s] = src_totals.get(s, 0) + n
    total_e = sum(src_totals.values()) or 1
    print("\n--- entry sources ---")
    for s, n in sorted(src_totals.items(), key=lambda kv: -kv[1]):
        print(f"{s:10s} {n:4d}  ({n / total_e:.0%})")
    all_db = sum(1 for r in done
                 if r.get("sources") and all(s in DB_SOURCES for s in r["sources"]))
    print(f"dishes fully DB-grounded: {all_db}/{len(done)}")

    cov = [r["n_entries"] / r["n_ingredients"] for r in done if r.get("n_ingredients")]
    if cov:
        print(f"ingredient coverage (entries/ingredients): median {statistics.median(cov):.2f}")

    suspects = [(r["dish_id"], s) for r in done for s in r.get("fallback_probe") or []
                if s["db_candidates"]]
    print(f"\n--- rule-#1 suspects (web/estimate despite DB candidates): {len(suspects)} ---")
    for dish_id, s in suspects[:10]:
        print(f"{dish_id}  {s['tier']:8s} {s['food']}  candidates: {'; '.join(s['db_candidates'])}")

    worst = sorted((r for r in done if r["gt"]["calories"]),
                   key=lambda r: -(ape(r["pipeline"]["calories"], r["gt"]["calories"]) or 0))[:5]
    print("\n--- worst 5 by pipeline calorie error ---")
    for r in worst:
        print(f"{r['dish_id']}  gt {r['gt']['calories']:.0f} cal -> got {r['pipeline']['calories']:.0f} "
              f"({ape(r['pipeline']['calories'], r['gt']['calories'])}%)  [{r['transcript'][:90]}]")


# ── Main ──────────────────────────────────────────────────────────────────────

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


def compare() -> None:
    """Aggregate every n5k_grounding*.jsonl into a variant x metrics matrix — the
    self-contained stand-in for an external experiment tracker."""
    import glob
    files = sorted(glob.glob(os.path.join(EVAL_DIR, "n5k_grounding*.jsonl")))
    variants = {}
    for path in files:
        rows = _load_file(path)
        if not rows:
            continue
        tag = rows[0].get("tag") or os.path.basename(path).replace("n5k_grounding", "").strip("_.jsonl") or "baseline"
        variants[tag] = rows

    if len(variants) < 1:
        print("no results found"); return
    print(f"{'variant':16s} {'n':>3s} {'model':22s} "
          + "  ".join(f"{m[:4]:>6s}" for m in MACROS) + "   cooked-cal  sources")
    for tag, rows in variants.items():
        model = (rows[0].get("model") or "?")[:22]
        meds = []
        for m in MACROS:
            e = [ape(r["pipeline"][m], r["gt"][m]) for r in rows if r["gt"][m]]
            e = [x for x in e if x is not None]
            meds.append(f"{statistics.median(e):6.1f}" if e else "   -- ")
        cooked = [ape(r["pipeline"]["calories"], r["gt"]["calories"]) for r in rows
                  if COOKED_RE.search(r["transcript"]) and r["gt"]["calories"]]
        cooked = [x for x in cooked if x is not None]
        cstr = f"{statistics.median(cooked):.1f}% (n={len(cooked)})" if cooked else "--"
        src = {}
        for r in rows:
            for s, n in (r.get("sources") or {}).items():
                src[s] = src.get(s, 0) + n
        print(f"{tag:16s} {len(rows):>3d} {model:22s} " + "  ".join(meds)
              + f"   {cstr:12s} {src}")

    # Head-to-head on shared dishes: baseline vs each other variant (cooked focus).
    base = {r["dish_id"]: r for r in variants.get("baseline", [])}
    for tag, rows in variants.items():
        if tag == "baseline" or not base:
            continue
        shared = [(base[r["dish_id"]], r) for r in rows if r["dish_id"] in base]
        cooked_pairs = [(b, v) for b, v in shared if COOKED_RE.search(b["transcript"])]
        print(f"\n--- baseline vs '{tag}' on {len(cooked_pairs)} shared COOKED dishes ---")
        improved = worsened = 0
        for b, v in cooked_pairs:
            be = ape(b["pipeline"]["calories"], b["gt"]["calories"])
            ve = ape(v["pipeline"]["calories"], v["gt"]["calories"])
            if be is None or ve is None:
                continue
            improved += ve < be - 1
            worsened += ve > be + 1
            if abs(ve - be) > 5:
                print(f"  {b['dish_id']}  gt {b['gt']['calories']:.0f}  base {b['pipeline']['calories']:.0f}"
                      f"({be:.0f}%) -> {tag} {v['pipeline']['calories']:.0f}({ve:.0f}%)  "
                      f"[{b['transcript'][:70]}]")
        print(f"  cooked dishes improved: {improved}  worsened: {worsened}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--compare", action="store_true", help="matrix across all variant files")
    ap.add_argument("--tag", default="", help="variant label -> n5k_grounding__<tag>.jsonl")
    ap.add_argument("--fresh", action="store_true", help="wipe scratch DB + results first")
    args = ap.parse_args()

    global OUT_PATH, RUN_TAG
    if args.tag:
        RUN_TAG = args.tag
        OUT_PATH = os.path.join(EVAL_DIR, f"n5k_grounding__{args.tag}.jsonl")
    if args.compare:
        compare()
        return
    if args.report:
        report()
        return
    if args.fresh:
        for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm", OUT_PATH):
            if os.path.exists(p):
                os.remove(p)

    from app.config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY missing")

    dishes = fetch_metadata()
    random.Random(42).shuffle(dishes)
    done = {r["dish_id"] for r in load_rows() if _ok(r)}   # only SUCCESSFUL dishes count as done
    todo = [d for d in dishes if d["dish_id"] not in done][: max(0, args.n - len(done))]
    print(f"eligible dishes: {len(dishes)}  already done: {len(done)}  running: {len(todo)}")
    if not todo:
        report()
        return

    uid = ensure_user()
    consec_err = 0
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, dish in enumerate(todo, 1):
            row = await run_dish(uid, dish)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            p = row.get("pipeline") or {}
            print(f"[{i}/{len(todo)}] {dish['dish_id']}  gt {dish['gt']['calories']:.0f} cal | "
                  f"pipe {p.get('calories', '--')} | base "
                  f"{(row.get('baseline') or {}).get('calories', '--')} | "
                  f"src {row.get('sources')} | {row.get('latency_s', '?')}s")
            consec_err = consec_err + 1 if not _ok(row) else 0
            if consec_err >= 3:
                print("\nABORTING: 3 consecutive failures (likely out of API credits or an "
                      "outage). Fix, then re-run — failed dishes retry automatically.")
                break
    report()


if __name__ == "__main__":
    asyncio.run(main())
