---
name: eval
description: Measure whether a change to Dictato's logging pipeline actually helped, without fooling yourself. Use before claiming any accuracy, grounding, portion, ranking, or STT improvement, and when designing an A/B or reading eval output.
---

# Measuring a change to Dictato

Every idea in this project that "looked great" and was wrong was caught by a
measurement, and every measurement that lied did so for a reason listed below.
Read this before running a harness, and before believing one.

## The harnesses

| Script | Ground truth | Costs API? |
|---|---|---|
| `eval_menumatch.py` | 41 restaurant dishes, dietitian calories | yes (~$1.10/arm) |
| `eval_grounding.py` | Nutrition5k, generic whole foods | yes |
| `eval_photo.py` / `eval_photo_real.py` | vision A/B | yes |
| `eval_search_ranking.py` | plausibility bands, food as EATEN | USDA only, free |
| `eval_stt_guard.py` | what a saved capture DID (logged / didn't) | free, local Whisper |
| `calibrate_confidence.py` | is the confidence flag honest | free |
| `backtest_portion_prior.py` | would the personal prior have fired | free |

`--tag <name>` writes `data/evals/<harness>__<tag>.jsonl`; `--compare` builds a
variant matrix. Everything writes to `data/evals/`, **never** the live DB.

Run the free ones first. They often answer the question.

## Ground truth

**A capture the user merely accepted is NOT ground truth.** People accept
whatever the card shows. Scoring a model against its own accepted output is
circular and will report whatever you hope. Legitimate ground truth here:
dietitian calories (Menu-Match), Nutrition5k measurements, plausibility bands
for a food as eaten, or a fact the server verified. `eval_stt_guard` is a good
pattern — its label is what the capture *did*, which no human assigned after
the fact.

## Traps that have actually bitten

1. **An unseeded scratch DB measures a crippled build.** `usda_portions` ships
   with the importer, not git. A scratch DB without it caches every food with no
   household measures — no count anchor, no snap ceiling — and the run "proves"
   your portion work did nothing. Seed it, then *assert* it:
   `DATABASE_PATH=<scratch> uv run python scripts/import_usda_reference.py`
   and fail the run if `SELECT COUNT(*) FROM usda_portions` is small.
   **Set `DATABASE_PATH` on the importer line itself** — without it the seed
   silently lands in the live DB and the scratch DB stays empty.
2. **`--fresh` deletes the scratch DB**, so seeding must happen AFTER it. Prefer
   deleting by hand, seeding, then running without `--fresh`.
3. **A shared DB across queries warms the cache**, so results depend on query
   ORDER. `eval_search_ranking` runs each query against a fresh temp database for
   exactly this reason; an earlier version shared one and flattered the fix.
4. **Aggregates hide the damage. Audit where a guard FIRES.** Snap v1 looked
   like a clean win until the eleven cases where it actually fired were read one
   by one — one was a real error. A ceiling that clamps correct portions is worse
   than no ceiling.
5. **Control your verification tools.** Run the checker against `HEAD` too. Two
   separate tools reported `static/app.js` was broken; both said the same about
   the committed file, so both were wrong (a naive bracket scanner, and esprima,
   which only parses ES2017).
6. **Check the credit balance before a multi-arm run, and run the cheap arm
   first.** An A/B once completed arm A, drained the balance, and arm B died on
   its first call with a 400. Cost is dominated by INPUT tokens — a 41-dish
   Menu-Match arm was 1.01M in vs 15.9k out, because each dish replays the whole
   tool loop every turn.
7. **A metric that calls the code under test is not comparable across arms.**
   Menu-Match's rule-#1 suspects come from `probe_db_candidates`, which calls
   `search_foods` — so each arm probes with ITS OWN search. Widening the
   candidate pool made the probe surface DB candidates it previously missed, and
   the suspect count rose from 2 to 6 without the agent behaving any worse. The
   denominator moved because of the change being measured. Before trusting any
   metric in an A/B, ask which parts of it the change touches; source
   distribution and calorie error were unaffected here and stayed comparable.
8. **Aim the eval at the corpus the change targets.** Ranking work on GENERIC
   foods ("oatmeal", "banana", "white rice") improved `eval_search_ranking`
   13/20 -> 17/20 and moved Menu-Match not at all — because Menu-Match is
   composite RESTAURANT dishes, where the DB genuinely lacks the item and the
   web/estimate fallback is supposed to fire. A flat result on the wrong corpus
   is not evidence of a failed change.

## Noise floors — do not report movement below these

- Menu-Match calorie error: **±5 points**. Restaurant portions genuinely vary,
  and a generic lookup legitimately differs from one restaurant's serving.
- `eval_search_ranking`: **17–19/20 is one number**, not a trend. It hits the
  live USDA API, whose results shift between runs.
- Any count over n=41 dishes: a change of one or two items is not a result.

Read Menu-Match in its own priority order: **rule-#1 suspects** (grounded in
`web`/`estimate` despite a real DB candidate) first, then source distribution,
then calorie error last.

## Designing an A/B

Both arms today, same model, same session. Use a git worktree for the "before"
arm so the comparison is code-vs-code with no cache-state or time confound:

```bash
git worktree add ../dictato-before <commit-before-your-first-change>
cp -r data/evals/menumatch ../dictato-before/data/evals/      # corpus
set -a; . ./.env; set +a                                      # keys, no second copy on disk
```

Run arms **sequentially** — concurrent arms race the USDA hourly cap and a
mid-run 429 corrupts one side silently. Remove the worktree when done
(`git worktree remove ../dictato-before`) so a second checkout of the repo, and
anything copied into it, does not linger.

## Reporting

State the noise floor next to the number. Say what the eval CANNOT cover — a
Menu-Match run says nothing about generic-food ranking or about behaviour that
only appears for a returning user with a warm cache. If one arm fails, report no
conclusion; half an A/B is not a weak result, it is not a result.
