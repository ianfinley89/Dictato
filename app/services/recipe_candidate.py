"""Spot captures that WANT to be a recipe, deterministically.

A homemade dish can share its name with a standard one and share almost nothing
else. Someone logged "chickpea curry", then said it was made with blended
tomatoes rather than coconut milk; the nearest database row is the coconut-milk
version, and no amount of re-ranking finds a row that does not exist. The honest
representation is a recipe built from DB-grounded ingredients — hard rule #1's
"decompose composite/homemade meals into DB-grounded ingredients".

The agent cannot build one today: it has search / create_food / log_food, and
`recipe_ingredients` is reachable only through the manual UI, which has produced
exactly one recipe ever. So before building that path, this marks the captures
that would have used it — a `recipe-candidate` tag that costs no tokens and can
be counted, exported and eyeballed.

Two signals, both from things already established:
  * the user described COMPOSITION (what it is made of, or what was swapped)
  * the capture did not decompose — one entry, or every entry a whole dish

Deliberately no model. Asking one whether something is a recipe would be a
self-report, and self-reports have already been measured worthless here
(portion confidence, AUROC 0.556).
"""
import re

# "made with X", "instead of Y", "no cheese", "I threw in", "my own recipe".
# Words that describe CONSTRUCTION, not just an adjective on a dish name.
_COMPOSITION_RE = re.compile(
    r"\b(made (?:with|from)|instead of|rather than|swapped?|substitut\w*|"
    r"homemade|home[- ]made|my own|my version|from scratch|leftover\w*|"
    r"i (?:made|cooked|threw|added|used)|recipe|"
    r"topped with|cooked in|fried in|mixed with|blended|"
    r"no (?:cheese|dairy|oil|butter|sugar|coconut|cream|mayo|dressing|sauce))\b",
    re.I,
)

def describes_composition(text: str) -> bool:
    """Did the user say what the dish is MADE OF, as opposed to naming it?

    An earlier version also read "with X and Y" as composition. It is not: "one
    biscuit with gravy and a sausage link" is a MEAL of three separate foods, and
    "cereal with milk and blackberries" was correctly logged as ten entries.
    Counting those tripled the fire rate with nothing but noise. Only explicit
    construction language counts."""
    return bool(_COMPOSITION_RE.search((text or "").strip()))


def is_candidate(transcript: str, entries: list[dict], corrections=None) -> bool:
    """True when this capture would have been better as a recipe.

    Composition was described, and the log did NOT reflect it — either nothing
    was decomposed, or a follow-up said this and changed nothing at all. The
    second case is the one worth catching: it looks like a clean capture in
    every other signal, because the entries are exactly as they were."""
    if not describes_composition(transcript):
        return False
    # A follow-up that landed nothing is the strongest case: it looks clean in
    # every other signal precisely because the entries did not move.
    if corrections and "correction:none-applied" in corrections:
        return True
    # Otherwise the description alone is enough. Counting entries was tried and
    # was wrong: "chickpea curry ... made with tomatoes" logged the curry AND a
    # pork tenderloin, so an entry-count test missed the very case this exists
    # for. A meal having several foods says nothing about whether one of them
    # is a homemade dish.
    return True


TAG = "recipe-candidate"


def tag_for(transcript: str, entries: list[dict], corrections=None) -> list[str]:
    """The tag to merge into a capture's annotation, or nothing."""
    return [TAG] if is_candidate(transcript, entries, corrections) else []
