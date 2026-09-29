"""Query rewriting: related-term expansion and the zero-result fallback ladder."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator

from .lexicon import Lexicon
from .models import SearchFilters
from .text import STOPWORDS, pluralize, stem, tokenize

COLORS = frozenset(
    "black white grey gray silver gold red blue green yellow orange purple pink brown beige navy "
    "teal tan cream clear transparent rose multicolor".split()
)
UNITS = frozenset(
    "gb tb mb mah w v watt watts volt volts oz lb lbs kg g mm cm m ft inch inches in qt l ml hz ghz "
    "pack pcs piece pieces count ct x".split()
)
# Words that introduce a detail clause: "keyboard WITH brown switches", "case FOR iphone 15".
CLAUSE_WORDS = frozenset("with without for compatible featuring including".split())
# Words that describe a product rather than name it. They're the first thing to drop when relaxing.
MODIFIERS = frozenset(
    "wireless wired portable rechargeable waterproof lightweight heavy duty mini large small medium "
    "extra slim ultra pro plus max smart digital electric cordless adjustable foldable folding "
    "compact premium professional quiet fast durable soft hard thick thin long short men mens women "
    "womens kids boys girls unisex adult baby vintage modern classic organic natural".split()
)


@dataclass(frozen=True)
class Rewrite:
    keywords: str
    filters: SearchFilters
    note: str


def normalize(query: str) -> str:
    """Canonical spacing/casing: "USB-C  16GB" -> "usb c 16 gb"."""
    return " ".join(tokenize(query))


def head_index(tokens: list[str]) -> int:
    """Index of the head noun, i.e. the thing being bought.

    In English product queries it's usually the last real word before any detail clause:
    "mechanical KEYBOARD with brown switches", "purple bluetooth SPEAKER 100 watt".
    """
    end = len(tokens)
    for i, t in enumerate(tokens):
        if i > 0 and t in CLAUSE_WORDS:
            end = i
            break
    for i in range(end - 1, -1, -1):
        t = tokens[i]
        if t.isalpha() and t not in STOPWORDS and t not in UNITS and t not in COLORS and t not in MODIFIERS:
            return i
    return max(0, end - 1)


def weighted_terms(query: str) -> list[tuple[str, float]]:
    """Content words of the query with how essential each is (1.0 = the head noun).

    Low-weight words are dropped first when a search has to be relaxed.
    """
    tokens = tokenize(query)
    if not tokens:
        return []
    h = head_index(tokens)
    clause = next((i for i, t in enumerate(tokens) if i > h and t in CLAUSE_WORDS), len(tokens))
    out = []
    for i, t in enumerate(tokens):
        if t in STOPWORDS or t in CLAUSE_WORDS:
            continue
        if i == h:
            w = 1.0
        elif not t.isalpha() or t in UNITS:
            w = 0.3
        elif t in COLORS:
            w = 0.4
        elif i > clause:
            w = 0.45  # details after "with"/"for" matter less than what comes before the noun
        elif t in MODIFIERS:
            w = 0.5
        else:
            w = 0.8
        out.append((t, w))
    return out


class QueryPlanner:
    def __init__(self, lexicon: Lexicon, max_related: int = 8) -> None:
        self.lexicon = lexicon
        self.max_related = max_related

    # ------------------------------------------------------------ spelling

    def spell_correct(self, query: str) -> tuple[str, list[tuple[str, str]]]:
        fixes: list[tuple[str, str]] = []
        out = []
        for tok in tokenize(query):
            fix = self.lexicon.correct(tok)
            if fix and fix != tok:
                fixes.append((tok, fix))
                out.append(fix)
            else:
                out.append(tok)
        return " ".join(out), fixes

    # ------------------------------------------------------------ related terms

    def related(self, query: str) -> list[Rewrite]:
        """Alternative phrasings that should find the same kind of product.

        Always run in thorough mode, even when the original query has results, so that items
        listed under a different name ("earphones" vs "headphones") are also found.
        """
        tokens = tokenize(query)
        seen = {normalize(query)}
        out: list[Rewrite] = []

        def add(tokens_: list[str], note: str) -> None:
            kw = " ".join(tokens_)
            if kw and kw not in seen:
                seen.add(kw)
                out.append(Rewrite(kw, SearchFilters(), note))

        # Phrase-level synonym and abbreviation swaps, longest phrase first ("cell phone" before "phone").
        for start, end in self._phrase_spans(tokens):
            span = tokens[start:end]
            key = tuple(stem(t) for t in span)
            phrase = " ".join(span)
            for alt in self.lexicon.synonyms(key):
                add(tokens[:start] + tokenize(alt) + tokens[end:], f'synonym: "{phrase}" -> "{alt}"')
            abbr = self.lexicon.abbreviation(key)
            if abbr:
                add(tokens[:start] + tokenize(abbr) + tokens[end:], f'abbreviation: "{phrase}" -> "{abbr}"')
            for alt in self.lexicon.narrower(key)[:2]:
                # Only swap in the specific term if it doesn't repeat a word already in the query.
                alt_toks = [t for t in tokenize(alt) if t not in tokens[:start] + tokens[end:]]
                add(tokens[:start] + alt_toks + tokens[end:], f'more specific: "{phrase}" -> "{alt}"')

        # Singular/plural of the head noun ("screwdriver set" vs "screwdriver sets").
        if tokens:
            h = head_index(tokens)
            word = tokens[h]
            alt = stem(word) if stem(word) != word else pluralize(word)
            if alt != word and not self.lexicon.correct(word):  # no point pluralizing a typo
                add(tokens[:h] + [alt] + tokens[h + 1 :], f'word form: "{word}" -> "{alt}"')

        # The query with filler words removed ("best cheap headphones for running").
        content = [t for t in tokens if t not in STOPWORDS]
        if content and len(content) < len(tokens):
            add(content, "removed filler words")

        return out[: self.max_related]

    def _phrase_spans(self, tokens: list[str]) -> Iterator[tuple[int, int]]:
        """Spans (start, end) of tokens that the lexicon knows, longest first, non-overlapping."""
        taken = [False] * len(tokens)
        for size in range(min(4, len(tokens)), 0, -1):
            for start in range(len(tokens) - size + 1):
                end = start + size
                if any(taken[start:end]):
                    continue
                key = tuple(stem(t) for t in tokens[start:end])
                if self.lexicon.synonyms(key) or self.lexicon.abbreviation(key) or self.lexicon.narrower(key):
                    for i in range(start, end):
                        taken[i] = True
                    yield start, end

    # ------------------------------------------------------------ fallback ladder

    def fallback_steps(self, query: str, filters: SearchFilters) -> Iterator[list[Rewrite]]:
        """Progressively looser rewrites, in the order they should be tried.

        Each yielded step is a batch of rewrites; the engine runs a whole step and stops as soon
        as a step produces results, so the least-distorted rewrite that works wins.
        """
        corrected, fixes = self.spell_correct(query)
        base = corrected if fixes else normalize(query)

        # 1. Spelling. Typos are the most common reason for zero results.
        if fixes:
            note = "spelling: " + ", ".join(f'"{a}" -> "{b}"' for a, b in fixes)
            yield [Rewrite(corrected, filters, note)]

        # 2. Filters. Keep the words, loosen the constraints one at a time, least important first.
        if not filters.is_empty():
            steps = []
            f = filters
            if f.min_rating is not None:
                f = replace(f, min_rating=None)
                steps.append(Rewrite(base, f, "dropped the minimum-rating filter"))
            if f.min_price is not None or f.max_price is not None:
                f = replace(f, min_price=None, max_price=None)
                steps.append(Rewrite(base, f, "dropped the price filter"))
            if f.search_index and f.search_index.lower() != "all":
                f = replace(f, search_index=None)
                steps.append(Rewrite(base, f, "searched all departments instead of one category"))
            for s in steps:
                yield [s]
        loose = SearchFilters(sort_by=filters.sort_by)

        # 3. Related phrasings of the (corrected) query, without filters.
        rel = self.related(base)
        if rel:
            yield [Rewrite(r.keywords, loose, r.note + " (no filters)" if not filters.is_empty() else r.note) for r in rel]

        # 4. Drop words, least essential first, one more each step, until only the head noun is left.
        terms = weighted_terms(base)
        if len(terms) > 1:
            h = max(range(len(terms)), key=lambda i: (terms[i][1], i))
            order = sorted((i for i in range(len(terms)) if i != h), key=lambda i: (terms[i][1], -i))
            dropped: list[int] = []
            for i in order:
                dropped.append(i)
                kept = [t for j, (t, _) in enumerate(terms) if j not in dropped]
                removed = [terms[j][0] for j in dropped]
                batch = [Rewrite(" ".join(kept), loose, "dropped: " + ", ".join(removed))]
                # Also try leaving out only this word, in case it alone was the culprit.
                if len(dropped) == 1 and len(terms) > 2:
                    for j in order[1:3]:
                        alt = [t for k, (t, _) in enumerate(terms) if k != j]
                        batch.append(Rewrite(" ".join(alt), loose, f"dropped: {terms[j][0]}"))
                yield batch

        # 5. A broader product type ("mechanical keyboard" -> "keyboard", "earbuds" -> "headphones").
        toks = [t for t, _ in terms]
        broader: list[Rewrite] = []
        for size in range(min(3, len(toks)), 0, -1):
            for start in range(len(toks) - size + 1):
                span = toks[start : start + size]
                wider = self.lexicon.broader(tuple(stem(t) for t in span))
                if wider:
                    broader.append(Rewrite(wider, loose, f'broader category: "{" ".join(span)}" -> "{wider}"'))
        if broader:
            yield broader[:3]

        # 6. Last resort: each meaningful word on its own, most important first.
        singles = sorted((t for t in terms if t[1] >= 0.5), key=lambda t: -t[1])
        if len(singles) > 1:
            yield [Rewrite(t, loose, f'searched for "{t}" alone') for t, _ in singles[:3]]
