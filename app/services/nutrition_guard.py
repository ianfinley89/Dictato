"""A plausibility guard for per-100g nutrition, applied wherever a `foods` row
is written (external cache, web/estimate creates) and as a one-time repair of
rows stored before the guard existed.

The database grounds nutrition (hard rule #1), but a database is only as good as
what got ingested. The two failure modes this catches are physical, not
model-driven, so the fix is deterministic and source-agnostic:

- **kJ read as kcal.** Open Food Facts stores energy in kilojoules; when the
  kcal field is missing the parser falls back to it, so 100g of ground beef
  (~250 kcal ≈ 1046 kJ) lands as "1046 calories". The tell is that energy is
  ~4.184x the calories implied by the macros (Atwater: 4·protein + 4·carbs +
  9·fat) — divide by 4.184.
- **Physically impossible energy.** Nothing real exceeds pure fat, ~884
  kcal/100g. Anything over ~900 is corrupt; recompute from the macros when they
  look sane, otherwise cap.

We only trust the Atwater signal when the macros carry real weight
(≥ `_ATWATER_MIN` kcal), which keeps the guard away from alcohol- or
polyol-heavy items whose calories legitimately exceed 4·4·9 (their macro sum is
near zero, so we leave them alone).
"""

KCAL_PER_KJ = 4.184          # 1 kcal = 4.184 kJ
_CAL_CEILING = 900.0         # pure fat is ~884 kcal/100g; nothing real is higher
_ATWATER_MIN = 50.0          # only trust the macro-derived energy above this
_KJ_LOW, _KJ_HIGH = 3.5, 5.0  # energy/Atwater band that means "this is kJ"


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def sanitize_per_100g(nutrients: dict) -> tuple[dict, str | None]:
    """Return (clean_nutrients, note). `note` is None when nothing changed and a
    short human-readable reason when the energy was corrected. Per-100g in and
    out; extra keys (e.g. `micros`) are preserved untouched."""
    n = dict(nutrients or {})
    protein = max(0.0, _f(n.get("protein_g")))
    carbs = max(0.0, _f(n.get("carbs_g")))
    fat = max(0.0, _f(n.get("fat_g")))
    cal = max(0.0, _f(n.get("calories")))
    atwater = 4 * protein + 4 * carbs + 9 * fat
    note = None

    if cal == 0 and atwater >= _ATWATER_MIN:
        cal = round(atwater, 1)
        note = "energy missing; computed from macros"
    elif cal > 0 and atwater >= _ATWATER_MIN and _KJ_LOW <= cal / atwater <= _KJ_HIGH:
        cal = round(cal / KCAL_PER_KJ, 1)
        note = "energy looked like kilojoules; converted to kcal"

    if cal > _CAL_CEILING:
        if 0 < atwater <= _CAL_CEILING:
            cal = round(atwater, 1)
            note = "energy exceeded the physical limit; recomputed from macros"
        else:
            cal = _CAL_CEILING
            note = "energy exceeded the physical limit; capped"

    n["calories"] = cal
    n["protein_g"] = protein
    n["carbs_g"] = carbs
    n["fat_g"] = fat
    if "fiber_g" in n:
        n["fiber_g"] = max(0.0, _f(n.get("fiber_g")))
    return n, note
