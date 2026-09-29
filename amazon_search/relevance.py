"""Score how well a product matches what the user originally typed."""

from __future__ import annotations

import difflib
import math

from .expansion import weighted_terms
from .lexicon import Lexicon
from .models import MatchType, Product
from .text import stem, tokenize

MATCH_BONUS = {MatchType.EXACT: 0.15, MatchType.RELATED: 0.05, MatchType.FALLBACK: 0.0}


# A word found only in the features/brand/category counts for less than one in the title:
# a laptop whose features mention its "RGB keyboard" is not a keyboard.
SECONDARY_FIELD_CREDIT = 0.6


def _stems(text: str) -> set[str]:
    return {stem(t) for t in tokenize(text)}


def _match(word: str, toks: set[str], lexicon: Lexicon) -> float:
    s = stem(word)
    if s in toks:
        return 1.0
    if lexicon.related_stems(word) & toks:
        return 0.75
    if word.isalpha() and len(word) >= 4 and difflib.get_close_matches(s, toks, n=1, cutoff=0.85):
        return 0.6
    return 0.0


def relevance(product: Product, query: str, lexicon: Lexicon) -> float:
    """0..1: weighted share of the query's words found in the product (exactly, as a synonym, or fuzzily)."""
    terms = weighted_terms(query)
    if not terms:
        return 0.0
    title = _stems(product.title)
    other = _stems(" ".join([product.brand or "", product.category or "", *product.features]))
    total = got = 0.0
    for word, weight in terms:
        total += weight
        got += weight * max(_match(word, title, lexicon), SECONDARY_FIELD_CREDIT * _match(word, other, lexicon))
    score = got / total if total else 0.0
    if " ".join(tokenize(query)) in " ".join(tokenize(product.title)):
        score = min(1.0, score + 0.1)  # whole phrase appears verbatim in the title
    return score


def rank_key(score: float, match_type: MatchType, product: Product) -> float:
    """Sort key: relevance first, then how directly it was found, then a little popularity."""
    popularity = 0.0
    if product.rating and product.review_count:
        popularity = (product.rating / 5) * min(1.0, math.log10(product.review_count + 1) / 5)
    return score + MATCH_BONUS[match_type] + 0.05 * popularity
