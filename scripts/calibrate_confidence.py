"""Is our portion-confidence signal REAL or decorative?

The result card tells users "~portion guessed" on low-confidence entries, and the
uncertainty layer wants to turn that into a number ("620 +/- 90 cal"). Both are
only honest if `portion_basis` actually predicts error. This measures it against
the eval corpus we already own, where every item has dietitian/scale ground truth.

Menu-Match is the calibration set: all 41 items resolve to exactly ONE entry per
item in every run, so item calorie error IS that entry's error — clean
attribution. Nutrition5k acts as the control arm: its transcripts state grams, so
those entries should be overwhelmingly high-basis AND low-error.

  uv run python scripts/calibrate_confidence.py

Reports: error distribution per basis bucket, AUROC (does low confidence rank
high-error items first?), risk-coverage (error on the most-confident subset), and
the empirical band multipliers an uncertainty layer should use.
"""
import json
import math
import os
import statistics
import sys

EVAL_DIR = os.path.join("data", "evals")
MM_RUNS = ("ladder", "snap", "snap2")
HIGH_ERR = 50.0          # an item this wrong is a materially bad log

# The ladder's own confidence mapping (portion.py), as a rank for scoring.
CONF_RANK = {"high": 2, "medium": 1, "low": 0}
BASIS_CONF = {"stated": "high", "label": "high", "count": "high",
              "household": "medium", "estimate": "low"}


def ape(pred: float, gt: float) -> float:
    return abs(pred - gt) / gt * 100


def load_mm() -> list[dict]:
    """One record per (run, item): its basis, confidence and calorie error."""
    out = []
    for tag in MM_RUNS:
        path = os.path.join(EVAL_DIR, f"menumatch__{tag}.jsonl")
        if not os.path.exists(path):
            continue
        seen = {}
        for line in open(path, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                seen[r["name"]] = r
        for r in seen.values():
            if r.get("errored") or r.get("pipeline_cal") is None:
                continue
            entries = r.get("entries") or []
            if len(entries) != 1:
                continue           # keep attribution clean
            e = entries[0]
            basis = e.get("basis") or "?"
            out.append({
                "run": tag, "item": r["name"], "basis": basis,
                "conf": BASIS_CONF.get(basis, "low"),
                "err": ape(r["pipeline_cal"], r["cal_gt"]),
                "source": e.get("source"), "snapped": bool(e.get("snapped")),
                "grams": e.get("grams"),
            })
    return out


def auroc(scores: list[float], labels: list[int]) -> float:
    """P(score of a positive > score of a negative), ties at 0.5 (Mann-Whitney)."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, math.ceil(q * len(v)) - 1)]


def main() -> None:
    rows = load_mm()
    if not rows:
        sys.exit("no Menu-Match runs with basis labels found")
    print(f"calibration set: {len(rows)} single-entry items across runs {MM_RUNS}\n")

    print("--- calorie error by portion basis (the core question) ---")
    print(f"{'basis':11s} {'conf':7s} {'n':>4s} {'median':>8s} {'mean':>8s} "
          f"{'P90':>8s} {'>50% err':>9s}")
    by_basis: dict[str, list[dict]] = {}
    for r in rows:
        by_basis.setdefault(r["basis"], []).append(r)
    for basis, rs in sorted(by_basis.items(), key=lambda kv: -CONF_RANK[BASIS_CONF.get(kv[0], "low")]):
        errs = [r["err"] for r in rs]
        bad = sum(1 for e in errs if e > HIGH_ERR)
        print(f"{basis:11s} {BASIS_CONF.get(basis, 'low'):7s} {len(rs):>4d} "
              f"{statistics.median(errs):>7.1f}% {statistics.mean(errs):>7.1f}% "
              f"{pct(errs, 0.9):>7.1f}% {bad:>4d}/{len(rs):<4d}")

    print("\n--- collapsed to the confidence the UI actually shows ---")
    for conf in ("high", "medium", "low"):
        errs = [r["err"] for r in rows if r["conf"] == conf]
        if errs:
            bad = sum(1 for e in errs if e > HIGH_ERR)
            print(f"{conf:7s} n={len(errs):<4d} median {statistics.median(errs):6.1f}%  "
                  f"mean {statistics.mean(errs):6.1f}%  materially-wrong rate {bad / len(errs):.0%}")

    # Does LOW confidence rank the bad logs first? (risk score = inverted conf)
    scores = [-CONF_RANK[r["conf"]] for r in rows]
    labels = [1 if r["err"] > HIGH_ERR else 0 for r in rows]
    a = auroc(scores, labels)
    print(f"\n--- AUROC: low confidence predicting a >{HIGH_ERR:.0f}% error item ---")
    print(f"AUROC {a:.3f}   (0.5 = the flag is noise; >0.7 = genuinely predictive)"
          f"   positives {sum(labels)}/{len(labels)}")

    print("\n--- risk-coverage: auto-accept only the confident, ask about the rest ---")
    hi = [r["err"] for r in rows if r["conf"] == "high"]
    lo = [r["err"] for r in rows if r["conf"] != "high"]
    if hi and lo:
        print(f"auto-accept high-conf ({len(hi) / len(rows):.0%} coverage): "
              f"median err {statistics.median(hi):.1f}%")
        print(f"flag the rest        ({len(lo) / len(rows):.0%} of items):  "
              f"median err {statistics.median(lo):.1f}%")
        print(f"separation: {statistics.median(lo) - statistics.median(hi):+.1f}pt median error")

    print("\n--- empirical band multipliers (for the uncertainty layer) ---")
    print("a band of +/- (median abs %err) covers ~half of cases; P90 ~ 90%:")
    for conf in ("high", "medium", "low"):
        errs = [r["err"] for r in rows if r["conf"] == conf]
        if errs:
            print(f"{conf:7s} +/-{statistics.median(errs):5.1f}% (50%)   "
                  f"+/-{pct(errs, 0.9):5.1f}% (90%)")

    # ── Retro-test the count-verification fix, free (no API calls) ────────────
    # verify_claims() only downgrades CONFIDENCE, never grams — so the recorded
    # errors stay valid and we can replay the new labelling over old runs.
    sys.path.insert(0, ".")
    from app.services.portion import stated_number
    print("\n=== RETRO-TEST: count verification (confidence-only, grams unchanged) ===")
    tr = {}
    for tag in MM_RUNS:
        path = os.path.join(EVAL_DIR, f"menumatch__{tag}.jsonl")
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                if line.strip():
                    r = json.loads(line)
                    tr[(tag, r["name"])] = r.get("transcript") or ""
    verified = sum(1 for k, v in tr.items() if stated_number(v))
    print(f"Menu-Match transcripts containing a real stated quantity: "
          f"{verified}/{len(tr)}  (they all say 'an ORDER of X' — a container, not a count)")

    for label, conf_of in (("before (shipped)", lambda r: r["conf"]),
                           ("after  (verified)", lambda r: r["conf"] if not (
                               r["basis"] == "count" and not stated_number(
                                   tr.get((r["run"], r["item"]), ""))) else "low")):
        hi = [r["err"] for r in rows if conf_of(r) == "high"]
        lo = [r["err"] for r in rows if conf_of(r) != "high"]
        if not hi:
            print(f"{label}: no high-confidence entries"); continue
        bad_hi = sum(1 for e in hi if e > HIGH_ERR)
        bad_lo = sum(1 for e in lo if e > HIGH_ERR)
        print(f"\n{label}")
        print(f"  claimed HIGH: n={len(hi):<4d} ({len(hi) / len(rows):>3.0%} coverage)  "
              f"median {statistics.median(hi):5.1f}%  BROKEN PROMISES "
              f"{bad_hi}/{len(hi)} = {bad_hi / len(hi):.0%}")
        print(f"  flagged LOW : n={len(lo):<4d}                  "
              f"median {statistics.median(lo):5.1f}%  materially wrong "
              f"{bad_lo}/{len(lo)} = {bad_lo / len(lo):.0%}")
        a2 = auroc([0 if conf_of(r) == "high" else 2 for r in rows], labels)
        print(f"  AUROC {a2:.3f}   lift (low/high wrong-rate) "
              f"{(bad_lo / len(lo)) / (bad_hi / len(hi)):.1f}x" if bad_hi else
              f"  AUROC {a2:.3f}   lift: infinite (zero broken promises)")

    # Direction of error per basis — under vs over, the lit's systematic-bias claim.
    print("\n--- signed bias by basis (lit says LLMs underestimate; ours?) ---")
    for tag in MM_RUNS:
        path = os.path.join(EVAL_DIR, f"menumatch__{tag}.jsonl")
        if not os.path.exists(path):
            continue
    signed: dict[str, list[float]] = {}
    for tag in MM_RUNS:
        path = os.path.join(EVAL_DIR, f"menumatch__{tag}.jsonl")
        if not os.path.exists(path):
            continue
        seen = {}
        for line in open(path, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                seen[r["name"]] = r
        for r in seen.values():
            if r.get("errored") or r.get("pipeline_cal") is None:
                continue
            entries = r.get("entries") or []
            if len(entries) != 1:
                continue
            b = entries[0].get("basis") or "?"
            signed.setdefault(b, []).append(
                (r["pipeline_cal"] - r["cal_gt"]) / r["cal_gt"] * 100)
    for b, vals in sorted(signed.items()):
        over = sum(1 for v in vals if v > 0)
        print(f"{b:11s} median {statistics.median(vals):+7.1f}%  "
              f"overestimated {over}/{len(vals)}")


if __name__ == "__main__":
    main()
