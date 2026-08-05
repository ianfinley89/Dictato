"""What a food's `source` actually means, declared once.

`source` is a single string carrying at least five orthogonal facts — who may see
the row, who may edit it, whether its numbers are genuinely per-100g, whether it
is trustworthy enough to learn statistics from, and how far to trust it against a
competing row. Each of those lived as its own tuple in its own module, and
nothing tied them together or said what a source IS.

That is how hard rule #1 got quietly inverted. The authority order — a food you
authored beats measured reference data beats the model's own guess — was never
declared anywhere. It was implied by a tuple in food_lookup, an ORDER BY clause
in a SQL string, and a helper function, and when `estimate` was grouped with
`user` and `recipe` for privacy it silently inherited their PRECEDENCE too. One
invented cereal then outranked every USDA row for a week.

So the properties are declared here and every consumer derives from them. Adding
a source becomes one row instead of a search for six tuples that must agree.

This is deliberately a plain dict of frozen dataclasses: no ontology framework,
no graph store, no new dependency. The value is having one place that is true,
not the notation it is written in.
"""
from dataclasses import dataclass

# Authority when two rows match a query equally well. Lower wins.
TRUST_AUTHORED = 0     # the user built this; for them it IS the answer
TRUST_REFERENCE = 1    # a real database, or web numbers with a citation
TRUST_INVENTED = 2     # the model's labelled last resort — never above real data


@dataclass(frozen=True)
class FoodSource:
    key: str
    label: str
    trust: int
    #: Visible only to the user who created it (mirrors the SQL privacy filter).
    private: bool = False
    #: The stored nutrition really is per 100 g, so the plausibility guard may
    #: correct it. FatSecret and user/recipe rows encode PER-SERVING values in
    #: the same slot — "correcting" those is what filed a batch recipe as 315 g
    #: of protein per 100 g.
    per_100g: bool = False
    #: Trustworthy enough to learn population statistics from (typical serving
    #: sizes, neighbour calorie bands). Reference data only — learning from the
    #: model's own guesses would be circular.
    reference_stats: bool = False
    #: The user may correct its name and macros. Shared reference rows must not
    #: be editable: one user's fix would rewrite everyone's data.
    editable: bool = False


_ALL = (
    FoodSource("usda", "USDA", TRUST_REFERENCE,
               per_100g=True, reference_stats=True),
    FoodSource("off", "Open Food Facts", TRUST_REFERENCE,
               per_100g=True, reference_stats=True),
    FoodSource("fatsecret", "FatSecret", TRUST_REFERENCE),
    FoodSource("manual", "Manual", TRUST_REFERENCE),
    FoodSource("web", "Web (published)", TRUST_REFERENCE, editable=True),
    FoodSource("user", "Custom (yours)", TRUST_AUTHORED,
               private=True, editable=True),
    FoodSource("recipe", "Recipe", TRUST_AUTHORED, private=True),
    FoodSource("estimate", "AI estimate", TRUST_INVENTED,
               private=True, editable=True),
)

SOURCES: dict[str, FoodSource] = {s.key: s for s in _ALL}

# An unrecognised source is treated as ordinary reference data that nobody may
# edit and nothing may be learned from — the conservative reading, so a source
# added to the database but not to this table cannot silently gain privileges.
_UNKNOWN = FoodSource("", "", TRUST_REFERENCE)


def get(source: str | None) -> FoodSource:
    return SOURCES.get(source or "", _UNKNOWN)


def _keys(pred) -> tuple[str, ...]:
    return tuple(s.key for s in _ALL if pred(s))


PRIVATE = _keys(lambda s: s.private)
AUTHORED = _keys(lambda s: s.trust == TRUST_AUTHORED)
INVENTED = _keys(lambda s: s.trust == TRUST_INVENTED)
PER_100G = _keys(lambda s: s.per_100g)
REFERENCE_STATS = _keys(lambda s: s.reference_stats)
EDITABLE = _keys(lambda s: s.editable)


def sql_list(keys: tuple[str, ...]) -> str:
    """Render keys as a SQL literal list for an `IN (...)` clause.

    Interpolation is safe here and only here: these are constants defined in
    this module, never anything a user typed."""
    return ", ".join(f"'{k}'" for k in keys)


def trust(source: str | None) -> int:
    return get(source).trust


def label(source: str | None) -> str:
    """Friendly 'where the nutrition came from'."""
    return get(source).label or (source or "Unknown").title()


def is_authored_by(food: dict, user_id: int) -> bool:
    """A food this user made on purpose — not one the model guessed for them.
    The distinction hard rule #1 turns on."""
    return (trust(food.get("source")) == TRUST_AUTHORED
            and food.get("created_by_user_id") == user_id)
