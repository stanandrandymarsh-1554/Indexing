"""The search engine: exact search, related-term expansion, and zero-result fallback."""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from .expansion import QueryPlanner, Rewrite, normalize
from .lexicon import Lexicon
from .models import Attempt, MatchType, Product, ScoredProduct, SearchFilters, SearchReport
from .providers.base import SearchProvider
from .relevance import rank_key, relevance


class SearchEngine:
    def __init__(
        self,
        provider: SearchProvider,
        lexicon: Optional[Lexicon] = None,
        *,
        pages: int = 2,
        thorough: bool = True,
        max_requests: int = 25,
        min_results: int = 1,
        min_score: float = 0.0,
    ) -> None:
        """
        pages        pages to fetch for the exact query (10 items per page on Amazon).
        thorough     also search synonyms/related phrasings even when the exact query has results.
        max_requests hard cap on provider calls per search, to respect API rate limits.
        min_results  run the fallback ladder when fewer than this many results were found.
        min_score    hide results whose relevance to the original query is below this (0..1).
        """
        self.provider = provider
        self.lexicon = lexicon or Lexicon.load()
        self.planner = QueryPlanner(self.lexicon)
        self.pages = max(1, min(pages, getattr(provider, "max_page", 10)))
        self.thorough = thorough
        self.max_requests = max_requests
        self.min_results = max(1, min_results)
        self.min_score = min_score
        self._cache: dict[tuple, list[Product]] = {}

    def search(self, query: str, filters: Optional[SearchFilters] = None, limit: int = 20) -> SearchReport:
        query = query.strip()
        if not normalize(query):
            raise ValueError("Search query is empty")
        filters = filters or SearchFilters()
        run = _Run(self, query)

        # Stage 1: the query exactly as typed, across several pages.
        for page in range(1, self.pages + 1):
            got = run.fetch("exact", Rewrite(query, filters, ""), page, MatchType.EXACT)
            if got is None or len(got) < 10:
                break  # out of budget, or no more pages

        # Stage 2: related phrasings (synonyms, abbreviations, word forms), same filters.
        if self.thorough:
            for rw in self.planner.related(query):
                run.fetch("related", Rewrite(rw.keywords, filters, rw.note), 1, MatchType.RELATED)

        # Stage 3: nothing (or too little) found -> climb the fallback ladder until something works.
        if len(run.found) < self.min_results:
            if not run.found:
                run.notes.append(f'No results for "{query}"; trying alternatives.')
            for step in self.planner.fallback_steps(query, filters):
                before = len(run.found)
                for rw in step:
                    run.fetch("fallback", rw, 1, MatchType.FALLBACK)
                if len(run.found) > before:
                    run.notes.extend(dict.fromkeys(rw.note for rw in step if rw.keywords in run.productive))
                if len(run.found) >= self.min_results or run.out_of_budget:
                    break
            if not run.found:
                run.notes.append("Nothing matched even after relaxing the search.")

        if run.out_of_budget:
            run.notes.append(f"Stopped after {self.max_requests} API requests (raise --max-requests to dig deeper).")

        results = [r for r in run.found.values() if r.score >= self.min_score]
        results.sort(key=lambda r: rank_key(r.score, r.match_type, r.product), reverse=True)
        return SearchReport(query=query, results=results[:limit], attempts=run.attempts, notes=run.notes)

    def _cached_search(self, keywords: str, filters: SearchFilters, page: int) -> tuple[list[Product], bool]:
        key = (normalize(keywords), tuple(sorted(asdict(filters).items())), page)
        if key in self._cache:
            return self._cache[key], True
        products = self.provider.search(keywords, filters, page)
        self._cache[key] = products
        return products, False


class _Run:
    """State for one call to SearchEngine.search."""

    def __init__(self, engine: SearchEngine, query: str) -> None:
        self.engine = engine
        self.query = query
        # Score against the spell-corrected query so typos don't sink good matches.
        self.scoring_query = engine.planner.spell_correct(query)[0]
        self.found: dict[str, ScoredProduct] = {}
        self.attempts: list[Attempt] = []
        self.notes: list[str] = []
        self.productive: set[str] = set()  # keywords that surfaced at least one new product
        self.requests = 0
        self._tried: set[tuple] = set()

    @property
    def out_of_budget(self) -> bool:
        return self.requests >= self.engine.max_requests

    def fetch(self, stage: str, rw: Rewrite, page: int, match: MatchType) -> Optional[list[Product]]:
        """Run one search and merge its products. Returns None if skipped for budget reasons."""
        key = (normalize(rw.keywords), tuple(sorted(asdict(rw.filters).items())), page)
        if key in self._tried:
            return []
        self._tried.add(key)
        if self.out_of_budget:
            return None
        products, cached = self.engine._cached_search(rw.keywords, rw.filters, page)
        if not cached:
            self.requests += 1
        self.attempts.append(Attempt(stage, rw.keywords, rw.filters, page, len(products), rw.note))
        self.engine.lexicon.learn((p.title for p in products), remember=True)
        for p in products:
            if p.asin and p.asin not in self.found:
                score = relevance(p, self.scoring_query, self.engine.lexicon)
                self.found[p.asin] = ScoredProduct(p, score, match, rw.keywords)
                self.productive.add(rw.keywords)
        return products
